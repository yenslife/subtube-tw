# SubTube TW

SubTube TW 是一個 YouTube 影片字幕翻譯與私人觀看自動化工具。

它可以從 YouTube URL 下載影片與多語字幕，使用 OpenAI-compatible LLM API（OpenAI 或 OpenRouter）根據上下文翻譯成台灣繁體中文字幕，產生 `.srt` 字幕檔，並可選擇將影片與字幕上傳到自己的 YouTube 頻道，再加入私人播放清單，方便在電視上的 YouTube App 觀看。

## Features

* 從 YouTube URL 下載影片
* 優先下載 QuickTime / YouTube 相容性較好的 H.264 + AAC MP4
* 下載多語字幕作為翻譯參考

  * `ko`
  * `en`
  * `ja`
  * `zh-Hant`
  * `zh-TW`
  * `zh-Hans`
  * `zh-CN`
* 自動選擇主要字幕來源
* 其他字幕作為 LLM reference
* 使用全片上下文摘要輔助翻譯
* 支援 OpenAI / OpenRouter provider 切換
* 支援平行翻譯 chunk
* 支援翻譯快取，避免失敗後整部影片重翻
* 產生台灣繁中 `zh-TW.srt`
* 產生 soft subtitle MP4
* 可選擇自動上傳影片與字幕到 YouTube
* 可選擇自動加入指定私人播放清單

## Project Structure

```text
subtube-tw/
├── yt_to_zh_video.sh          # 主 workflow
├── translate_srt.py           # SRT 上下文翻譯工具
├── llm_client.py              # OpenAI / OpenRouter provider 設定與 OpenAI-compatible client
├── generate_upload_metadata.py # 自動產生 YouTube title / description
├── upload_to_youtube.py       # YouTube 上傳工具
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
├── client_secret.json         # 本機 OAuth secret，不要 commit
├── token.json                 # 本機 OAuth token，不要 commit
└── youtube_translation_*/     # 每次執行產生的輸出資料夾
```

## Requirements

### System Dependencies

macOS:

```bash
brew install yt-dlp ffmpeg uv
```

或至少需要：

```bash
yt-dlp
ffmpeg
python >= 3.12
uv
```

### Python Dependencies

Dependencies are defined in `pyproject.toml`; install them with:

```bash
uv sync
```

## Environment Setup

複製 `.env.example`：

```bash
cp .env.example .env
```

### OpenAI provider（預設）

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY='your_openai_api_key'
OPENAI_MODEL='gpt-4.1-mini'
OPENAI_SUMMARY_MODEL='gpt-4.1-mini'
```

### OpenRouter provider

```bash
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY='your_openrouter_api_key'
OPENROUTER_MODEL='google/gemini-2.5-flash-lite'
OPENROUTER_SUMMARY_MODEL='google/gemini-2.5-flash-lite'
# stronger mode: google/gemini-2.5-flash
```

OpenRouter 模型名稱要使用 `provider/model` 格式，例如：

```bash
OPENROUTER_MODEL='anthropic/claude-3.5-haiku'
OPENROUTER_MODEL='google/gemini-2.5-flash-lite'
OPENROUTER_MODEL='openai/gpt-4.1-mini'
```

常用設定：

```bash
MAX_WORKERS=5
MAX_RETRIES=2
LLM_TIMEOUT_SECONDS=60
LLM_MAX_OUTPUT_TOKENS=16000
CHUNK_MAX_CHARS=6000
CHUNK_GAP_SECONDS=2.0
CHUNK_OVERLAP_SECONDS=120.0
REFERENCE_WINDOW_SECONDS=6.0
MAX_SPLIT_RETRY_DEPTH=2
```

### YouTube bot check / cookies

如果 `yt-dlp` 出現：

```text
Sign in to confirm you’re not a bot
```

請從已登入 YouTube 的瀏覽器匯出 **Netscape cookies**，放到專案根目錄，例如：

```text
youtube.cookies.txt
```

Chrome / Chromium 可以使用：

* [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc?hl=zh-tw)

匯出時請確認是 `cookies.txt` / Netscape 格式；這個檔案等同登入憑證，不要傳到不信任的地方。

然後在 `.env` 設定：

```bash
YTDLP_COOKIES_FILE=youtube.cookies.txt
SUBTITLE_LANGS="en ko ja"
MIN_REFERENCE_SUBTITLES=2
```

或在同一台有瀏覽器 profile 的電腦執行時使用：

```bash
YTDLP_COOKIES_FROM_BROWSER=firefox
# YTDLP_COOKIES_FROM_BROWSER=chrome
```

`youtube.cookies.txt` 等同登入憑證，已被 `.gitignore` 忽略，請不要 commit 或公開分享。

若 YouTube 的 JS challenge 讓 yt-dlp 只列出 storyboard 圖片格式，請安裝 Node.js。腳本會自動把 node 傳給 yt-dlp；必要時也可在 `.env` 手動指定：

```bash
YTDLP_JS_RUNTIME=node:/path/to/node
YTDLP_REMOTE_COMPONENTS=ejs:github
```

### Troubleshooting notes

| 問題 | 原因 | 解法 |
|---|---|---|
| `Sign in to confirm you’re not a bot` | YouTube 要求登入狀態 | 匯出 `youtube.cookies.txt`，設定 `YTDLP_COOKIES_FILE` |
| `Only images are available for download` / 只剩 storyboard | YouTube `n` challenge 沒解開 | 安裝 Node.js；腳本會自動加 `--js-runtimes node:...` 與 `--remote-components ejs:github` |
| `.env: line ... command not found` | `.env` 有未加引號的空白值；不能直接 `source` | 腳本已改成安全 `KEY=VALUE` parser，不會執行 `.env` |
| 上傳階段找不到 `client_secret.json` | 從輸出資料夾執行時相對路徑跑掉 | `upload_to_youtube.py` 已改成相對路徑以專案根目錄為基準 |

## Basic Usage

執行：

```bash
./yt_to_zh_video.sh 'https://youtu.be/VIDEO_ID?si=xxxx'
```

完成後會產生一個新的工作資料夾，例如：

```text
youtube_translation_20260627_024434/
├── input.mp4
├── input.ko.srt
├── input.en.srt
├── zh-TW.srt
└── output_zh_softsub.mp4
```

其中：

* `input.mp4`：原始下載影片
* `input.*.srt`：YouTube 下載到的字幕
* `zh-TW.srt`：翻譯後的台灣繁中字幕
* `output_zh_softsub.mp4`：內含可開關字幕軌的 MP4

## Translation Workflow

主要流程：

```text
YouTube URL
  ↓
yt-dlp 下載影片
  ↓
yt-dlp 下載多語字幕
  ↓
自動選主要字幕
  ↓
建立全片上下文摘要
  ↓
平行翻譯字幕 chunks
  ↓
輸出 zh-TW.srt
  ↓
ffmpeg 合併成 soft subtitle MP4
```

字幕來源優先順序：

```text
en > ko > ja > zh-Hant > zh-TW > zh-Hans > zh-CN
```

如果只有英文字幕，也可以直接從英文翻成台灣繁中。

## Performance Tuning

### 平行翻譯數量

```bash
export MAX_WORKERS=5
```

建議值：

```text
5：預設，平行 API 呼叫最快；搭配 MAX_RETRIES=2 / LLM_TIMEOUT_SECONDS=60 避免長時間卡住
2-3：provider 不穩或 rate limit 時降速
1：除錯用，最容易定位是哪個 chunk 出問題
```

### 調整 chunk / overlap

```bash
export CHUNK_MAX_CHARS=6000
export CHUNK_GAP_SECONDS=2.0
export CHUNK_OVERLAP_SECONDS=120.0
export REFERENCE_WINDOW_SECONDS=6.0
```

`CHUNK_MAX_CHARS` 是實際要求模型輸出的 target chunk 大小；`CHUNK_OVERLAP_SECONDS` 是前後文，只供理解、不要求輸出；`REFERENCE_WINDOW_SECONDS` 控制其他語言字幕用 timecode overlap 額外帶入的秒數。

### 快取

翻譯結果會存在：

```text
.cache_translate/
```

如果翻譯到一半失敗，重新執行時已完成的 chunk 會直接使用快取，不會重新消耗 API。

## YouTube Upload Setup

如果想把影片上傳到自己的 YouTube 頻道，並加到私人播放清單，需要先設定 Google OAuth。

### 1. Google Cloud Console

1. 建立 Google Cloud Project
2. Enable `YouTube Data API v3`
3. 建立 OAuth Client ID
4. Application type 選 `Desktop app`
5. 下載 OAuth JSON
6. 放到專案根目錄並命名為：

```text
client_secret.json
```

第一次上傳時會開瀏覽器登入授權，成功後會產生：

```text
token.json
```

這兩個檔案都不要 commit。

## Upload to YouTube

設定播放清單 ID：

```bash
export YOUTUBE_PLAYLIST_ID='PLxxxxxxxxxxxxxxxx'
```

啟用自動上傳：

```bash
export UPLOAD_YOUTUBE=1
export YOUTUBE_PRIVACY=private
```

預設上傳 metadata：

* Title：`(赫米翻譯) 原始影片標題`
* Description：`generate_upload_metadata.py` 會用目前 LLM provider 根據原始 metadata 與 `zh-TW.srt` 自動產生台灣繁中簡介。

如果要手動覆寫，才設定：

```bash
export YOUTUBE_TITLE='Custom translated title'
export YOUTUBE_DESCRIPTION='Custom description.'
```

執行：

```bash
./yt_to_zh_video.sh 'https://youtu.be/VIDEO_ID?si=xxxx'
```

完成後會：

1. 上傳 `input.mp4`
2. 上傳 `zh-TW.srt`
3. 將影片設為 private
4. 加入指定播放清單

建議上傳 `input.mp4` 並上傳 `zh-TW.srt` 作為 YouTube 字幕軌，而不是上傳 `output_zh_softsub.mp4`。這樣可以在 YouTube / TV App 中直接使用字幕功能，之後修字幕也不需要重新上傳影片。

## Manual Upload

如果影片已經翻譯完成，可以手動執行：

```bash
uv run ./upload_to_youtube.py \
  --video youtube_translation_YYYYMMDD_HHMMSS/input.mp4 \
  --srt youtube_translation_YYYYMMDD_HHMMSS/zh-TW.srt \
  --title "Translated Video" \
  --description "Private translated copy for personal viewing." \
  --playlist-id "$YOUTUBE_PLAYLIST_ID" \
  --privacy private
```

## QuickTime Compatibility

SubTube TW 預設會盡量下載：

```text
Video: H.264 / avc1
Audio: AAC / mp4a
Container: mp4
```

這比 YouTube 預設可能下載到的 AV1 + Opus 更適合 QuickTime Player。

如果遇到 QuickTime 顯示：

```text
此檔案包含部分與 QuickTime Player 不相容的媒體。
```

可以檢查：

```bash
ffprobe input.mp4
```

若看到：

```text
Video: av1
Audio: opus
```

代表需要轉成 H.264 + AAC：

```bash
ffmpeg -y \
  -i output_zh_softsub.mp4 \
  -map 0 \
  -c:v libx264 \
  -pix_fmt yuv420p \
  -profile:v high \
  -level 4.1 \
  -c:a aac \
  -b:a 160k \
  -c:s mov_text \
  -movflags +faststart \
  output_zh_softsub_quicktime.mp4
```

## Common Issues

### `HTTP Error 429: Too Many Requests`

YouTube 字幕下載端點有時會限制請求。SubTube TW 會逐語言下載字幕，某個語言失敗會略過，不會中斷整個流程。

如果頻繁發生，可以把 `yt_to_zh_video.sh` 裡的：

```bash
sleep 2
```

改成：

```bash
sleep 5
```

### `REFERENCE_ARGS[@]: unbound variable`

代表目前沒有 reference 字幕，但 shell 在 `set -u` 下展開空陣列失敗。請確認 `yt_to_zh_video.sh` 使用 command array 呼叫 `translate_srt.py`，不要直接展開空的 `REFERENCE_ARGS[@]`。

### `ERROR: 找不到 client_secret.json`

代表上傳腳本找不到 OAuth client secret。請確認：

```text
client_secret.json
```

放在專案根目錄，或設定：

```bash
export YOUTUBE_CLIENT_SECRET_FILE=/absolute/path/to/client_secret.json
```

### 有些字幕沒有翻譯

如果 log 出現：

```text
WARNING: missing translated IDs
```

代表模型漏掉部分字幕編號。目前 fallback 會保留原文，避免 SRT 壞掉。若想降低漏翻機率，可以：

```bash
export CHUNK_MAX_CHARS=6000
export MAX_WORKERS=3
```

重新執行時會使用快取。

## Security Notes

請勿 commit：

```text
.env
client_secret.json
token.json
.cache_translate/
youtube_translation_*/
*.mp4
*.srt
```

尤其：

* `OPENAI_API_KEY`
* `client_secret.json`
* `token.json`

都屬於敏感資訊。

## License

Personal automation project. Add a license if you plan to publish it.

