#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load local .env when present.
# This intentionally parses KEY=VALUE instead of `source` so `.env` is data, not executable shell.
# It also tolerates unquoted values with spaces, e.g. YOUTUBE_TITLE=Translated YouTube Video.
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    # Trim leading whitespace after '='.
    value="${value#${value%%[![:space:]]*}}"
    # Remove one pair of matching surrounding quotes if present.
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$SCRIPT_DIR/.env"
fi

URL="${1:-}"

if [[ -z "$URL" ]]; then
  echo "Usage: $0 <youtube_url>"
  exit 1
fi

YTDLP_ARGS=()
if [[ -n "${YTDLP_COOKIES_FILE:-}" ]]; then
  YTDLP_ARGS+=(--cookies "$YTDLP_COOKIES_FILE")
fi
if [[ -n "${YTDLP_COOKIES_FROM_BROWSER:-}" ]]; then
  YTDLP_ARGS+=(--cookies-from-browser "$YTDLP_COOKIES_FROM_BROWSER")
fi

# YouTube sometimes requires JS challenge solving. yt-dlp may not auto-detect node
# in non-interactive shells, so pass it explicitly when available.
if [[ -n "${YTDLP_JS_RUNTIME:-}" ]]; then
  YTDLP_ARGS+=(--js-runtimes "$YTDLP_JS_RUNTIME")
elif command -v node >/dev/null 2>&1; then
  YTDLP_ARGS+=(--js-runtimes "node:$(command -v node)")
fi

if [[ -n "${YTDLP_REMOTE_COMPONENTS:-}" ]]; then
  YTDLP_ARGS+=(--remote-components "$YTDLP_REMOTE_COMPONENTS")
elif command -v node >/dev/null 2>&1; then
  YTDLP_ARGS+=(--remote-components ejs:github)
fi

WORKDIR="${WORKDIR:-youtube_translation_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

echo "===== 1. download video and metadata ====="

yt-dlp "${YTDLP_ARGS[@]}" \
  -f "${YTDLP_VIDEO_FORMAT:-bv*[vcodec^=avc1][ext=mp4]+ba[acodec^=mp4a][ext=m4a]/b[ext=mp4][vcodec^=avc1][acodec^=mp4a]/b[ext=mp4]}" \
  --merge-output-format mp4 \
  --write-info-json \
  -o "input.%(ext)s" \
  "$URL"

if [[ -f "input.info.json" ]]; then
  cp input.info.json metadata.json
fi

if [[ ! -f "metadata.json" ]]; then
  echo "ERROR: 找不到 metadata.json"
  exit 1
fi

echo "Original title: $(python3 - <<'PY'
import json
from pathlib import Path
metadata = json.loads(Path('metadata.json').read_text(encoding='utf-8'))
print(metadata.get('title') or metadata.get('fulltitle') or 'YouTube Video')
PY
)"

VIDEO="input.mp4"

if [[ ! -f "$VIDEO" ]]; then
  echo "ERROR: 找不到 input.mp4"
  exit 1
fi

echo
echo "===== 2. download subtitles one by one ====="

read -r -a LANGS <<< "${SUBTITLE_LANGS:-en ko ja}"
MIN_REFERENCE_SUBTITLES="${MIN_REFERENCE_SUBTITLES:-2}"

have_primary_and_refs() {
  local primary=""
  local refs=0
  local lang candidate srt
  for lang in "${LANGS[@]}"; do
    candidate="$(ls -1 *."$lang".srt 2>/dev/null | head -n 1 || true)"
    if [[ -n "$candidate" ]]; then
      primary="$candidate"
      break
    fi
  done
  [[ -n "$primary" ]] || return 1
  for srt in *.srt; do
    [[ -e "$srt" ]] || continue
    [[ "$srt" == "$primary" ]] && continue
    refs=$((refs + 1))
  done
  [[ "$refs" -ge "$MIN_REFERENCE_SUBTITLES" ]]
}

for lang in "${LANGS[@]}"; do
  if have_primary_and_refs; then
    echo "Have primary subtitle and $MIN_REFERENCE_SUBTITLES reference subtitle(s), stop downloading more subtitles."
    break
  fi

  echo
  echo "----- subtitle lang: $lang -----"

  if ls -1 *."$lang".srt >/dev/null 2>&1; then
    echo "Already have subtitle for $lang, skip."
    continue
  fi

  set +e
  timeout "${YTDLP_SUBTITLE_TIMEOUT_SECONDS:-120}" yt-dlp "${YTDLP_ARGS[@]}" \
    --skip-download \
    --write-sub \
    --write-auto-sub \
    --sub-langs "$lang" \
    --convert-subs srt \
    -o "input.%(ext)s" \
    "$URL"
  rc=$?
  set -e

  if [[ "$rc" -ne 0 ]]; then
    echo "WARNING: subtitle download failed for $lang, skip."
  fi

  # 避免太密集打 YouTube 字幕端點，降低 429 機率
  sleep 5
done

echo
echo "Downloaded subtitles:"
ls -1 *.srt 2>/dev/null || true

echo
echo "===== 3. choose primary subtitle ====="

PRIMARY_SRT=""
PRIMARY_LANG=""

for lang in "${LANGS[@]}"; do
  candidate="$(ls -1 *."$lang".srt 2>/dev/null | head -n 1 || true)"
  if [[ -n "$candidate" ]]; then
    PRIMARY_SRT="$candidate"
    PRIMARY_LANG="$lang"
    break
  fi
done

if [[ -z "$PRIMARY_SRT" ]]; then
  echo "ERROR: 找不到可用字幕。"
  echo
  echo "你可以先檢查可用字幕："
  echo "yt-dlp --list-subs '$URL'"
  exit 1
fi

echo "Primary subtitle: $PRIMARY_SRT ($PRIMARY_LANG)"

REFERENCE_ARGS=()

for srt in *.srt; do
  [[ "$srt" == "$PRIMARY_SRT" ]] && continue
  REFERENCE_ARGS+=(--reference "$srt")
done

if [[ "${#REFERENCE_ARGS[@]}" -gt 0 ]]; then
  echo "Reference subtitles:"
  for srt in *.srt; do
    [[ "$srt" == "$PRIMARY_SRT" ]] && continue
    echo "  - $srt"
  done
else
  echo "Reference subtitles: none"
fi

echo
echo "===== 4. translate subtitle to zh-TW with context ====="

TRANSLATE_CMD=(
  uv run --project "$SCRIPT_DIR" "$SCRIPT_DIR/translate_srt.py"
  "$PRIMARY_SRT"
  "zh-TW.srt"
  --source-lang "$PRIMARY_LANG"
  --workers "${MAX_WORKERS:-5}"
)

if [[ "${#REFERENCE_ARGS[@]}" -gt 0 ]]; then
  TRANSLATE_CMD+=("${REFERENCE_ARGS[@]}")
fi

"${TRANSLATE_CMD[@]}"

echo
echo "===== 5. mux soft subtitle into mp4 ====="

ffmpeg -y \
  -i "$VIDEO" \
  -i "zh-TW.srt" \
  -map 0:v \
  -map 0:a \
  -map 1 \
  -c:v copy \
  -c:a copy \
  -c:s mov_text \
  -metadata:s:s:0 language=chi \
  "output_zh_softsub.mp4"

echo
echo "DONE:"
echo "$PWD/output_zh_softsub.mp4"
echo "$PWD/zh-TW.srt"

if [[ "${UPLOAD_YOUTUBE:-1}" == "1" ]]; then
  if [[ -z "${YOUTUBE_PLAYLIST_ID:-}" ]]; then
    echo "ERROR: UPLOAD_YOUTUBE=1 但沒有設定 YOUTUBE_PLAYLIST_ID"
    exit 1
  fi

  echo
  echo "===== 6. generate upload metadata ====="

  uv run --project "$SCRIPT_DIR" "$SCRIPT_DIR/generate_upload_metadata.py" \
    --metadata-json "metadata.json" \
    --srt "zh-TW.srt" \
    --source-url "$URL" \
    --output "upload_metadata.json"

  echo "Upload title: $(python3 - <<'PY'
import json
from pathlib import Path
print(json.loads(Path('upload_metadata.json').read_text(encoding='utf-8'))['title'])
PY
)"

  echo
  echo "===== 7. upload to YouTube ====="

  uv run --project "$SCRIPT_DIR" "$SCRIPT_DIR/upload_to_youtube.py" \
    --video "$VIDEO" \
    --srt "zh-TW.srt" \
    --title "$(python3 - <<'PY'
import json
from pathlib import Path
print(json.loads(Path('upload_metadata.json').read_text(encoding='utf-8'))['title'])
PY
)" \
    --description "$(python3 - <<'PY'
import json
from pathlib import Path
print(json.loads(Path('upload_metadata.json').read_text(encoding='utf-8'))['description'])
PY
)" \
    --playlist-id "$YOUTUBE_PLAYLIST_ID" \
    --privacy "${YOUTUBE_PRIVACY:-private}"
fi
