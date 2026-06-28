#!/usr/bin/env python3
"""Generate YouTube upload title/description for translated videos."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from llm_client import LLM_TIMEOUT_SECONDS, get_llm_config, make_llm_client

LLM_CONFIG = get_llm_config()
MODEL = LLM_CONFIG.summary_model
MAX_CHARS = int(os.getenv("UPLOAD_DESCRIPTION_SUBTITLE_CHARS", "16000"))
MAX_TITLE_LEN = int(os.getenv("YOUTUBE_TITLE_MAX_CHARS", "100"))
OLD_TITLE_PLACEHOLDER = "Translated YouTube Video"
OLD_DESCRIPTION_PLACEHOLDER = "Private translated copy for personal viewing."


def env_override(name: str, placeholder: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value if value and value != placeholder else None


def parse_srt_text(path: Path, max_chars: int) -> str:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    lines: list[str] = []
    total = 0
    for block in blocks:
        parts = block.strip().splitlines()
        if len(parts) < 3 or not parts[0].strip().isdigit() or "-->" not in parts[1]:
            continue
        item = f"[{parts[0].strip()}] {parts[1].strip()} {' '.join(p.strip() for p in parts[2:] if p.strip())}"
        if total + len(item) + 1 > max_chars:
            break
        lines.append(item)
        total += len(item) + 1
    return "\n".join(lines)


def original_title(metadata: dict) -> str:
    return (
        metadata.get("title")
        or metadata.get("fulltitle")
        or metadata.get("alt_title")
        or "YouTube Video"
    ).strip()


def truncate_title(title: str) -> str:
    title = " ".join(title.split()).strip()
    return title if len(title) <= MAX_TITLE_LEN else title[: max(0, MAX_TITLE_LEN - 1)].rstrip() + "…"


def fallback_description(metadata: dict, source_url: str) -> str:
    title = original_title(metadata)
    uploader = metadata.get("uploader") or metadata.get("channel") or "原頻道"
    return (
        f"這是「{title}」的台灣繁中字幕翻譯版本。\n\n"
        f"原影片頻道：{uploader}\n"
        f"原影片連結：{source_url}\n\n"
        "字幕由 SubTube TW / 赫米流程自動翻譯產生，內容可能仍需人工校對。"
    )


def call_llm_description(metadata: dict, srt_sample: str, source_url: str) -> str:
    prompt = f"""請根據以下 YouTube 原始 metadata 與台灣繁中字幕 sample，寫一段適合放在 YouTube 上傳 description 的台灣繁中簡介。

要求：
- 2 到 4 句話，簡潔自然。
- 說明這支影片大概在講什麼，不要逐段摘要。
- 不要自稱你是 AI，也不要寫「以下是」。
- 保留原始專有名詞；中文用台灣慣用語。
- 最後附上原影片連結與自動翻譯提示。

原始標題：{original_title(metadata)}
頻道：{metadata.get("uploader") or metadata.get("channel") or ""}
片長：{metadata.get("duration_string") or metadata.get("duration") or ""}
原影片連結：{source_url}

原始 description 節錄：
{(metadata.get("description") or "").strip()[:3000] or "(無)"}

台灣繁中字幕 sample：
{srt_sample}
"""

    client = make_llm_client(LLM_CONFIG)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "你是熟悉台灣繁中語氣的 YouTube 影片介紹文案編輯。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        timeout=LLM_TIMEOUT_SECONDS,
    )
    text = (resp.choices[0].message.content or "").strip()
    if source_url not in text:
        text += f"\n\n原影片連結：{source_url}"
    if "自動翻譯" not in text and "字幕" not in text:
        text += "\n\n字幕由 SubTube TW / 赫米流程自動翻譯產生，內容可能仍需人工校對。"
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-json", required=True)
    parser.add_argument("--srt", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    metadata = json.loads(Path(args.metadata_json).read_text(encoding="utf-8"))
    title = env_override("YOUTUBE_TITLE", OLD_TITLE_PLACEHOLDER) or truncate_title(
        f"(赫米翻譯) {original_title(metadata)}"
    )

    description = env_override("YOUTUBE_DESCRIPTION", OLD_DESCRIPTION_PLACEHOLDER)
    if not description:
        try:
            description = call_llm_description(
                metadata,
                parse_srt_text(Path(args.srt), MAX_CHARS),
                args.source_url,
            )
        except Exception as exc:
            print(
                f"WARNING: failed to generate upload description with LLM ({LLM_CONFIG.provider}, model={MODEL}): {exc}",
                file=sys.stderr,
            )
            description = fallback_description(metadata, args.source_url)

    result = {"title": title, "description": description}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
