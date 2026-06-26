# AGENTS.md

## Project: SubTube TW

SubTube TW 是一個本機 YouTube 字幕翻譯 workflow。

目標流程：

```text
YouTube URL
→ 下載影片與可用字幕
→ 使用 OpenAI API 根據上下文翻成台灣繁中 SRT
→ 產生 soft subtitle MP4
→ 可選：上傳影片與字幕到使用者自己的 YouTube 頻道
→ 可選：加入私人播放清單，方便在電視 YouTube App 播放
```

## Important Files

```text
yt_to_zh_video.sh          # 主 workflow shell script
translate_srt.py           # 使用 OpenAI 翻譯 SRT
upload_to_youtube.py       # 上傳影片、字幕、加入 playlist
pyproject.toml             # Python dependencies
.env.example               # 環境變數範本
.gitignore                 # 忽略 secrets / outputs / media
```

## Main Commands

### Run full translation workflow

```bash
./yt_to_zh_video.sh 'https://youtu.be/VIDEO_ID?si=xxxx'
```

輸出會放在自動建立的資料夾：

```text
youtube_translation_YYYYMMDD_HHMMSS/
├── input.mp4
├── input.*.srt
├── zh-TW.srt
└── output_zh_softsub.mp4
```

### Continue from existing folder

```bash
cd youtube_translation_YYYYMMDD_HHMMSS

uv run --project .. ../translate_srt.py \
  input.ko.srt \
  zh-TW.srt \
  --source-lang ko \
  --reference input.en.srt \
  --workers 5

ffmpeg -y \
  -i input.mp4 \
  -i zh-TW.srt \
  -map 0:v \
  -map 0:a \
  -map 1 \
  -c:v copy \
  -c:a copy \
  -c:s mov_text \
  -metadata:s:s:0 language=chi \
  output_zh_softsub.mp4
```

## Environment Variables

必要：

```bash
OPENAI_API_KEY=
```

常用：

```bash
OPENAI_MODEL=gpt-4.1-mini
OPENAI_SUMMARY_MODEL=gpt-4.1-mini
MAX_WORKERS=5
CHUNK_MAX_CHARS=8000
REFERENCE_WINDOW_BEFORE=40
REFERENCE_WINDOW_AFTER=40
SUMMARY_MAX_CHARS_PER_FILE=20000
CACHE_DIR=.cache_translate
```

YouTube upload：

```bash
UPLOAD_YOUTUBE=1
YOUTUBE_PLAYLIST_ID=PLxxxxxxxxxxxxxxxx
YOUTUBE_TITLE='Translated Video'
YOUTUBE_DESCRIPTION='Private translated copy for personal viewing.'
YOUTUBE_PRIVACY=private
YOUTUBE_CLIENT_SECRET_FILE=client_secret.json
YOUTUBE_TOKEN_FILE=token.json
```

## Translation Behavior

`translate_srt.py` does:

1. Parse primary SRT.
2. Build global context summary.
3. Split subtitles into chunks.
4. Use nearby reference subtitles if available.
5. Translate chunks in parallel.
6. Cache chunk results in `.cache_translate/`.
7. Merge translations back into valid SRT.

Primary subtitle priority in `yt_to_zh_video.sh`:

```text
ko > en > ja > zh-Hant > zh-TW > zh-Hans > zh-CN
```

Reference subtitles are all other downloaded `.srt` files.

If the model misses subtitle IDs, the script keeps original text as fallback to avoid broken SRT.

## YouTube Upload Behavior

`upload_to_youtube.py`:

1. Uploads `input.mp4`.
2. Uploads `zh-TW.srt` as caption track.
3. Adds uploaded video to `YOUTUBE_PLAYLIST_ID`.

Prefer uploading `input.mp4` + `zh-TW.srt`, not `output_zh_softsub.mp4`, because YouTube captions are easier to toggle and fix.

Requires Google OAuth files:

```text
client_secret.json
token.json
```

Do not commit these.

## Known Issues

### HTTP 429 while downloading subtitles

YouTube may rate-limit subtitle downloads. Current shell script downloads subtitles one language at a time and skips failed languages.

If too frequent:

```bash
sleep 2
```

in `yt_to_zh_video.sh` can be increased to:

```bash
sleep 5
```

### QuickTime incompatible media

Avoid AV1/Opus. The video download format should prefer H.264 + AAC:

```bash
-f "bv*[vcodec^=avc1][ext=mp4]+ba[acodec^=mp4a][ext=m4a]/b[ext=mp4][vcodec^=avc1][acodec^=mp4a]/b[ext=mp4]"
```

### Empty reference array bug

When no reference subtitles exist, do not directly expand an empty array under `set -u`.

Use command array pattern:

```bash
TRANSLATE_CMD=(uv run --project "$SCRIPT_DIR" "$SCRIPT_DIR/translate_srt.py" ...)
if [[ "${#REFERENCE_ARGS[@]}" -gt 0 ]]; then
  TRANSLATE_CMD+=("${REFERENCE_ARGS[@]}")
fi
"${TRANSLATE_CMD[@]}"
```

## Do Not Commit

```text
.env
client_secret.json
token.json
.cache_translate/
youtube_translation_*/
*.mp4
*.srt
*.vtt
.DS_Store
```

## Agent Rules

When modifying this project:

1. Keep scripts resumable.
2. Avoid re-downloading or re-translating if output/cache already exists.
3. Preserve Taiwan Traditional Chinese output style.
4. Prefer YouTube caption upload over hard subtitle video.
5. Do not expose API keys, OAuth secrets, or tokens.
6. When changing shell scripts, keep `set -euo pipefail`.
7. Be careful with empty bash arrays under `set -u`.
8. Prefer H.264 + AAC for compatibility.
9. Keep generated media and subtitles out of git.
10. Keep instructions short and operational.

