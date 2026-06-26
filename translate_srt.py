#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from openai import OpenAI


MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
SUMMARY_MODEL = os.getenv("OPENAI_SUMMARY_MODEL", MODEL)

CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "8000"))
REFERENCE_WINDOW_BEFORE = int(os.getenv("REFERENCE_WINDOW_BEFORE", "40"))
REFERENCE_WINDOW_AFTER = int(os.getenv("REFERENCE_WINDOW_AFTER", "40"))
SUMMARY_MAX_CHARS_PER_FILE = int(os.getenv("SUMMARY_MAX_CHARS_PER_FILE", "20000"))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))

# 平行翻譯數量，建議 2~4
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3"))

CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache_translate"))

client = OpenAI()


@dataclass
class SubtitleItem:
    idx: str
    timecode: str
    body: str


def parse_srt(text: str) -> list[SubtitleItem]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n\s*\n", text, flags=re.M)

    items: list[SubtitleItem] = []

    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue

        idx = lines[0].strip()
        timecode = lines[1].strip()
        body = "\n".join(line.strip() for line in lines[2:]).strip()

        if not idx.isdigit():
            continue

        if "-->" not in timecode:
            continue

        if not body:
            continue

        items.append(SubtitleItem(idx=idx, timecode=timecode, body=body))

    return items


def read_srt(path: Path) -> list[SubtitleItem]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return parse_srt(text)


def cleanup_subtitle_text(text: str) -> str:
    text = text.strip()
    text = text.strip("「」")

    if not text:
        return "..."

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def write_srt(path: Path, items: list[SubtitleItem], translations: dict[str, str]) -> None:
    out_blocks = []

    for item in items:
        zh = translations.get(item.idx, item.body).strip()
        zh = cleanup_subtitle_text(zh)
        out_blocks.append(f"{item.idx}\n{item.timecode}\n{zh}")

    path.write_text("\n\n".join(out_blocks) + "\n", encoding="utf-8")


def chunk_items(items: list[SubtitleItem], max_chars: int) -> Iterable[list[SubtitleItem]]:
    buf: list[SubtitleItem] = []
    size = 0

    for item in items:
        entry = f"[{item.idx}]\n{item.body}\n\n"
        entry_size = len(entry)

        if buf and size + entry_size > max_chars:
            yield buf
            buf = []
            size = 0

        buf.append(item)
        size += entry_size

    if buf:
        yield buf


def call_openai(messages: list[dict], model: str, temperature: float = 0.2) -> str:
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            print(
                f"OpenAI API error, retry {attempt}/{MAX_RETRIES} after {wait}s: {e}",
                file=sys.stderr,
            )
            time.sleep(wait)

    raise RuntimeError(f"OpenAI API failed after retries: {last_err}")


def items_to_numbered_text(items: list[SubtitleItem], max_chars: int | None = None) -> str:
    parts = []

    for item in items:
        parts.append(f"[{item.idx}]\n{item.body}")

    text = "\n\n".join(parts)

    if max_chars is not None:
        return text[:max_chars]

    return text


def build_reference_map(reference_paths: list[Path]) -> dict[str, list[SubtitleItem]]:
    refs = {}

    for path in reference_paths:
        if not path.exists():
            print(f"WARNING: reference not found: {path}", file=sys.stderr)
            continue

        try:
            refs[path.name] = read_srt(path)
        except Exception as e:
            print(f"WARNING: failed to read reference {path}: {e}", file=sys.stderr)

    return refs


def make_global_context(
    primary_path: Path,
    primary_items: list[SubtitleItem],
    reference_map: dict[str, list[SubtitleItem]],
    source_lang: str,
) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_key_src = {
        "type": "global_context",
        "model": SUMMARY_MODEL,
        "primary": primary_path.name,
        "source_lang": source_lang,
        "primary_sample": items_to_numbered_text(primary_items, max_chars=SUMMARY_MAX_CHARS_PER_FILE),
        "references": {
            name: items_to_numbered_text(items, max_chars=SUMMARY_MAX_CHARS_PER_FILE)
            for name, items in reference_map.items()
        },
    }

    cache_key = hashlib.sha256(
        json.dumps(cache_key_src, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    cache_path = CACHE_DIR / f"global_{cache_key}.txt"

    if cache_path.exists():
        print("global context cache hit", file=sys.stderr)
        return cache_path.read_text(encoding="utf-8")

    primary_sample = cache_key_src["primary_sample"]

    ref_parts = []
    for name, items in reference_map.items():
        ref_parts.append(
            f"Reference subtitle file: {name}\n"
            + items_to_numbered_text(items, max_chars=SUMMARY_MAX_CHARS_PER_FILE)
        )

    refs_text = "\n\n---\n\n".join(ref_parts)

    prompt = f"""請先閱讀以下影片字幕資料，建立翻譯用的全片上下文。

你不需要翻譯整份字幕。請輸出一份「給字幕翻譯員用的上下文筆記」，內容包含：
1. 影片主題與大致情境
2. 出現的人物、團體、稱呼、關係
3. 專有名詞、節目名稱、地名、品牌、梗
4. 語氣風格：正式/口語/搞笑/訪談/競賽等
5. 翻成台灣繁中時應固定使用的譯名表
6. 可能的自動字幕錯誤與判讀建議

主要來源字幕語言代碼：{source_lang}
主要來源字幕檔：{primary_path.name}

主要來源字幕 sample：
{primary_sample}

其他語言字幕 reference：
{refs_text if refs_text else "(無)"}
"""

    messages = [
        {
            "role": "system",
            "content": "你是專業影音字幕翻譯企劃，擅長整理跨語言字幕的上下文與譯名表。",
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    print("building global context...", file=sys.stderr)
    context = call_openai(messages, model=SUMMARY_MODEL, temperature=0.2)
    cache_path.write_text(context, encoding="utf-8")
    print("global context built", file=sys.stderr)

    return context


def nearest_reference_items(
    ref_items: list[SubtitleItem],
    start_idx: int,
    end_idx: int,
    before: int,
    after: int,
) -> list[SubtitleItem]:
    lo = max(1, start_idx - before)
    hi = end_idx + after

    out = []

    for item in ref_items:
        try:
            idx = int(item.idx)
        except ValueError:
            continue

        if lo <= idx <= hi:
            out.append(item)

    return out


def build_reference_block_for_chunk(
    reference_map: dict[str, list[SubtitleItem]],
    chunk: list[SubtitleItem],
) -> str:
    if not reference_map:
        return ""

    start_idx = int(chunk[0].idx)
    end_idx = int(chunk[-1].idx)

    parts = []

    for name, items in reference_map.items():
        nearby = nearest_reference_items(
            items,
            start_idx=start_idx,
            end_idx=end_idx,
            before=REFERENCE_WINDOW_BEFORE,
            after=REFERENCE_WINDOW_AFTER,
        )

        if not nearby:
            continue

        parts.append(
            f"Reference file: {name}\n"
            + items_to_numbered_text(nearby)
        )

    return "\n\n---\n\n".join(parts)


def parse_translated_numbered_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}

    pattern = re.compile(
        r"\[(\d+)\]\s*\n(.*?)(?=\n\s*\[\d+\]\s*\n|\Z)",
        flags=re.S,
    )

    for match in pattern.finditer(text.strip()):
        idx = match.group(1)
        body = match.group(2).strip()
        if body:
            result[idx] = cleanup_subtitle_text(body)

    return result


def chunk_cache_key(
    chunk: list[SubtitleItem],
    global_context: str,
    reference_block: str,
    source_lang: str,
) -> str:
    src = {
        "type": "chunk_translation",
        "model": MODEL,
        "source_lang": source_lang,
        "start_idx": chunk[0].idx,
        "end_idx": chunk[-1].idx,
        "items": [(item.idx, item.timecode, item.body) for item in chunk],
        "global_context": global_context,
        "reference_block": reference_block,
    }

    return hashlib.sha256(
        json.dumps(src, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def translate_chunk(
    chunk: list[SubtitleItem],
    global_context: str,
    reference_block: str,
    source_lang: str,
) -> dict[str, str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    start_idx = chunk[0].idx
    end_idx = chunk[-1].idx

    cache_key = chunk_cache_key(
        chunk=chunk,
        global_context=global_context,
        reference_block=reference_block,
        source_lang=source_lang,
    )

    cache_path = CACHE_DIR / f"chunk_{start_idx}_{end_idx}_{cache_key}.json"

    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"cache hit chunk {start_idx}-{end_idx}", file=sys.stderr)
            return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass

    payload = items_to_numbered_text(chunk)
    ref_text = reference_block.strip() if reference_block.strip() else "(無)"

    prompt = f"""你是專業影音字幕翻譯員。

目標：
把「主要來源字幕」翻成自然的台灣繁體中文。

重要規則：
1. 必須保留每個 [字幕編號]。
2. 不要輸出時間軸。
3. 每個編號只輸出翻譯後文字。
4. 用台灣自然口語，不要使用中國用語。
5. 不要逐字硬翻；要根據全片上下文、前後字幕、reference 字幕理解意思。
6. 若自動字幕有重複、斷句錯、語助詞太多，請整理成順暢短句。
7. 不要加註解、不要括號補充、不要說明。
8. 不要省略任何字幕編號。
9. 若某句是人名、笑聲、語氣詞，也要給合適的繁中字幕。
10. 若 reference 與主要來源衝突，以主要來源為主；reference 只用來輔助理解。

主要來源語言代碼：{source_lang}

全片上下文筆記：
{global_context}

目前片段附近的其他語言 reference：
{ref_text}

輸出格式必須如下：
[1]
翻譯文字

[2]
翻譯文字

主要來源字幕：
{payload}
"""

    messages = [
        {
            "role": "system",
            "content": "你是精準的台灣繁中字幕翻譯助手。只輸出指定格式，不要解釋。",
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    translated_text = call_openai(messages, model=MODEL, temperature=0.2)
    parsed = parse_translated_numbered_text(translated_text)

    missing = [item.idx for item in chunk if item.idx not in parsed]

    if missing:
        print(
            f"WARNING: missing translated IDs in chunk {start_idx}-{end_idx}: "
            + ", ".join(missing[:20])
            + (" ..." if len(missing) > 20 else ""),
            file=sys.stderr,
        )

    cache_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return parsed


def translate_chunk_job(
    chunk_no: int,
    total_chunks: int,
    chunk: list[SubtitleItem],
    global_context: str,
    reference_map: dict[str, list[SubtitleItem]],
    source_lang: str,
) -> tuple[int, dict[str, str]]:
    start_idx = chunk[0].idx
    end_idx = chunk[-1].idx

    print(
        f"start chunk {chunk_no}/{total_chunks}: subtitle {start_idx}-{end_idx}",
        file=sys.stderr,
    )

    reference_block = build_reference_block_for_chunk(reference_map, chunk)

    parsed = translate_chunk(
        chunk=chunk,
        global_context=global_context,
        reference_block=reference_block,
        source_lang=source_lang,
    )

    print(
        f"done chunk {chunk_no}/{total_chunks}: subtitle {start_idx}-{end_idx}, translated={len(parsed)}",
        file=sys.stderr,
    )

    return chunk_no, parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate SRT subtitles to Taiwan Traditional Chinese with optional reference subtitles."
    )
    parser.add_argument("input_srt", help="Primary source subtitle SRT")
    parser.add_argument("output_srt", help="Output zh-TW SRT")
    parser.add_argument(
        "--source-lang",
        default="unknown",
        help="Primary source language code, e.g. ko/en/ja",
    )
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="Reference subtitle SRT. Can be used multiple times.",
    )
    parser.add_argument(
        "--no-global-context",
        action="store_true",
        help="Disable global context summary step.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help="Parallel chunk translation workers.",
    )

    args = parser.parse_args()

    src = Path(args.input_srt)
    dst = Path(args.output_srt)
    reference_paths = [Path(p) for p in args.reference]

    if not src.exists():
        print(f"ERROR: input SRT not found: {src}", file=sys.stderr)
        sys.exit(1)

    primary_items = read_srt(src)

    if not primary_items:
        print(f"ERROR: no subtitle items parsed from {src}", file=sys.stderr)
        sys.exit(1)

    reference_map = build_reference_map(reference_paths)

    print(f"primary subtitle: {src}", file=sys.stderr)
    print(f"primary items: {len(primary_items)}", file=sys.stderr)
    print(f"reference files: {len(reference_map)}", file=sys.stderr)
    print(f"workers: {args.workers}", file=sys.stderr)

    if args.no_global_context:
        global_context = "未建立全片上下文。請依目前片段與 reference 字幕翻譯。"
    else:
        global_context = make_global_context(
            primary_path=src,
            primary_items=primary_items,
            reference_map=reference_map,
            source_lang=args.source_lang,
        )

    chunks = list(chunk_items(primary_items, max_chars=CHUNK_MAX_CHARS))
    total_chunks = len(chunks)

    print(f"total chunks: {total_chunks}", file=sys.stderr)

    translations: dict[str, str] = {}

    if args.workers <= 1:
        for i, chunk in enumerate(chunks, start=1):
            _, parsed = translate_chunk_job(
                chunk_no=i,
                total_chunks=total_chunks,
                chunk=chunk,
                global_context=global_context,
                reference_map=reference_map,
                source_lang=args.source_lang,
            )
            translations.update(parsed)
            print(
                f"translated items so far: {len(translations)}/{len(primary_items)}",
                file=sys.stderr,
            )
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = []

            for i, chunk in enumerate(chunks, start=1):
                futures.append(
                    executor.submit(
                        translate_chunk_job,
                        i,
                        total_chunks,
                        chunk,
                        global_context,
                        reference_map,
                        args.source_lang,
                    )
                )

            for future in as_completed(futures):
                chunk_no, parsed = future.result()
                translations.update(parsed)
                print(
                    f"collected chunk {chunk_no}/{total_chunks}; translated items so far: {len(translations)}/{len(primary_items)}",
                    file=sys.stderr,
                )

    missing_count = 0

    for item in primary_items:
        if item.idx not in translations:
            translations[item.idx] = item.body
            missing_count += 1

    if missing_count:
        print(
            f"WARNING: {missing_count} subtitles were not translated; kept original text.",
            file=sys.stderr,
        )

    write_srt(dst, primary_items, translations)

    print(f"wrote {dst}", file=sys.stderr)


if __name__ == "__main__":
    main()
