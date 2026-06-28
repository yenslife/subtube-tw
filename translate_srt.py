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

from llm_client import LLM_MAX_OUTPUT_TOKENS, LLM_TIMEOUT_SECONDS, get_llm_config, make_llm_client


LLM_CONFIG = get_llm_config()
MODEL = LLM_CONFIG.model
SUMMARY_MODEL = LLM_CONFIG.summary_model

TRANSLATION_PROMPT_VERSION = "v11-target-json-same-id"
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "12000"))
CHUNK_GAP_SECONDS = float(os.getenv("CHUNK_GAP_SECONDS", "2.0"))
CHUNK_OVERLAP_SECONDS = float(os.getenv("CHUNK_OVERLAP_SECONDS", "120.0"))
REFERENCE_WINDOW_SECONDS = float(os.getenv("REFERENCE_WINDOW_SECONDS", "6.0"))
REFERENCE_WINDOW_BEFORE = int(os.getenv("REFERENCE_WINDOW_BEFORE", "40"))  # legacy fallback only
REFERENCE_WINDOW_AFTER = int(os.getenv("REFERENCE_WINDOW_AFTER", "40"))  # legacy fallback only
SUMMARY_MAX_CHARS_PER_FILE = int(os.getenv("SUMMARY_MAX_CHARS_PER_FILE", "20000"))
MAX_SPLIT_RETRY_DEPTH = int(os.getenv("MAX_SPLIT_RETRY_DEPTH", "2"))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "1"))

CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache_translate"))

client = make_llm_client(LLM_CONFIG)


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


def _parse_timecode(tc: str) -> float:
    """Parse SRT timecode 'HH:MM:SS,mmm' to seconds."""
    tc = tc.strip().replace(".", ",")
    h, m, rest = tc.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms[:3].ljust(3, "0")) / 1000


def _item_times(item: SubtitleItem) -> tuple[float, float]:
    start, end = item.timecode.split("-->", 1)
    return _parse_timecode(start), _parse_timecode(end)


def _item_start_seconds(item: SubtitleItem) -> float:
    try:
        return _item_times(item)[0]
    except Exception:
        return 0.0


def chunk_items(items: list[SubtitleItem], max_chars: int) -> Iterable[list[SubtitleItem]]:
    buf: list[SubtitleItem] = []
    size = 0
    prev_end: float | None = None

    for item in items:
        entry = f"[{item.idx}]\n{item.body}\n\n"
        entry_size = len(entry)

        try:
            start, end = _item_times(item)
        except Exception:
            start, end = 0.0, 0.0

        gap = (start - prev_end) if prev_end is not None else 0.0

        # Prefer a natural cut at a speech/time gap once the chunk is big enough.
        if buf and gap >= CHUNK_GAP_SECONDS and size >= max_chars * 0.6:
            yield buf
            buf = []
            size = 0

        # Hard cap if no good gap appears.
        if buf and size + entry_size > max_chars:
            yield buf
            buf = []
            size = 0

        buf.append(item)
        size += entry_size
        prev_end = end or start

    if buf:
        yield buf


def call_openai(
    messages: list[dict],
    model: str,
    temperature: float = 0.2,
    response_format: dict | None = None,
) -> str:
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": LLM_MAX_OUTPUT_TOKENS,
                "timeout": LLM_TIMEOUT_SECONDS,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
                if LLM_CONFIG.provider == "openrouter":
                    kwargs["extra_body"] = {"provider": {"require_parameters": True}}

            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content.strip()
        except Exception as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            print(
                f"LLM API error ({LLM_CONFIG.provider}, model={model}), retry {attempt}/{MAX_RETRIES} after {wait}s: {e}",
                file=sys.stderr,
            )
            time.sleep(wait)

    raise RuntimeError(f"LLM API failed after retries ({LLM_CONFIG.provider}, model={model}): {last_err}")


def items_to_numbered_text(items: list[SubtitleItem], max_chars: int | None = None) -> str:
    parts = []

    for item in items:
        parts.append(f"[{item.idx}]\n{item.body}")

    text = "\n\n".join(parts)

    if max_chars is not None:
        return text[:max_chars]

    return text


def items_to_target_json(items: list[SubtitleItem]) -> str:
    rows = [
        {"id": item.idx, "timecode": item.timecode, "source_text": item.body}
        for item in items
    ]
    return json.dumps(rows, ensure_ascii=False, indent=2)


def items_to_context_text(items: list[SubtitleItem], max_chars: int | None = None, prefix: str = "ctx") -> str:
    parts = []

    for item in items:
        parts.append(f"{prefix} {item.idx}: {item.body}")

    text = "\n".join(parts)

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


def context_items_for_chunk(
    all_items: list[SubtitleItem],
    chunk: list[SubtitleItem],
    overlap_seconds: float,
) -> list[SubtitleItem]:
    try:
        chunk_start = _item_times(chunk[0])[0]
        chunk_end = _item_times(chunk[-1])[1]
    except Exception:
        return chunk

    lo = max(0.0, chunk_start - overlap_seconds)
    hi = chunk_end + overlap_seconds

    out: list[SubtitleItem] = []
    for item in all_items:
        try:
            start, end = _item_times(item)
        except Exception:
            continue
        if start <= hi and end >= lo:
            out.append(item)
    return out


def outside_chunk_context_text(
    context_items: list[SubtitleItem],
    chunk: list[SubtitleItem],
) -> str:
    target_ids = {item.idx for item in chunk}
    outside = [item for item in context_items if item.idx not in target_ids]
    return items_to_context_text(outside, prefix="ctx") if outside else "(無)"


def make_global_context(
    primary_path: Path,
    primary_items: list[SubtitleItem],
    reference_map: dict[str, list[SubtitleItem]],
    source_lang: str,
) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_key_src = {
        "type": "global_context",
        "provider": LLM_CONFIG.provider,
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
    chunk_start_sec: float = 0.0,
    chunk_end_sec: float = 0.0,
) -> list[SubtitleItem]:
    """Find reference items by time overlap, not subtitle index.

    Different language tracks use different subtitle numbering, so index-based
    matching can feed the model unrelated English/Japanese lines. Timecode is
    the stable join key. Index matching is only a last-resort fallback.
    """
    if chunk_start_sec > 0 or chunk_end_sec > 0:
        lo_sec = max(0.0, chunk_start_sec - REFERENCE_WINDOW_SECONDS)
        hi_sec = chunk_end_sec + REFERENCE_WINDOW_SECONDS if chunk_end_sec > 0 else float("inf")

        out = []
        for item in ref_items:
            try:
                item_start, item_end = _item_times(item)
            except Exception:
                continue

            if item_start <= hi_sec and item_end >= lo_sec:
                out.append(item)

        return out

    # Fallback for malformed/no timecodes.
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

    # Compute chunk time range for time-based reference matching
    chunk_start_sec = 0.0
    chunk_end_sec = 0.0
    try:
        chunk_start_sec = _item_times(chunk[0])[0]
        chunk_end_sec = _item_times(chunk[-1])[1]
    except Exception:
        pass

    parts = []

    for name, items in reference_map.items():
        nearby = nearest_reference_items(
            items,
            start_idx=start_idx,
            end_idx=end_idx,
            before=REFERENCE_WINDOW_BEFORE,
            after=REFERENCE_WINDOW_AFTER,
            chunk_start_sec=chunk_start_sec,
            chunk_end_sec=chunk_end_sec,
        )

        if not nearby:
            continue

        parts.append(
            f"Reference file: {name}\n"
            + items_to_context_text(nearby, prefix="ref")
        )

    return "\n\n---\n\n".join(parts)


def parse_translated_numbered_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}

    pattern = re.compile(
        r"\[(\d+)\]\s*(.*?)(?=\n\s*\[\d+\]\s*|\Z)",
        flags=re.S,
    )

    for match in pattern.finditer(text.strip()):
        idx = match.group(1)
        body = match.group(2).strip()
        if body:
            result[idx] = cleanup_subtitle_text(body)

    return result


def translation_response_format(expected_count: int | None = None) -> dict:
    translations_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Original subtitle ID."},
                "text": {"type": "string", "description": "Taiwan Traditional Chinese subtitle text for the same ID only."},
            },
            "required": ["id", "text"],
            "additionalProperties": False,
        },
    }

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "subtitle_translations",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "translations": translations_schema,
                },
                "required": ["translations"],
                "additionalProperties": False,
            },
        },
    }


def parse_structured_translations(text: str) -> dict[str, str]:
    data = json.loads(text)
    rows = data.get("translations")
    if not isinstance(rows, list):
        raise ValueError("structured response missing translations array")

    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = str(row.get("id", "")).strip()
        body = str(row.get("text", "")).strip()
        if idx and body:
            result[idx] = cleanup_subtitle_text(body)
    return result


def chunk_cache_key(
    chunk: list[SubtitleItem],
    global_context: str,
    primary_context_block: str,
    reference_block: str,
    source_lang: str,
) -> str:
    src = {
        "type": "chunk_translation",
        "output_format": "json_schema",
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "chunk_max_chars": CHUNK_MAX_CHARS,
        "chunk_gap_seconds": CHUNK_GAP_SECONDS,
        "chunk_overlap_seconds": CHUNK_OVERLAP_SECONDS,
        "reference_window_seconds": REFERENCE_WINDOW_SECONDS,
        "provider": LLM_CONFIG.provider,
        "model": MODEL,
        "source_lang": source_lang,
        "start_idx": chunk[0].idx,
        "end_idx": chunk[-1].idx,
        "items": [(item.idx, item.timecode, item.body) for item in chunk],
        "global_context": global_context,
        "primary_context_block": primary_context_block,
        "reference_block": reference_block,
    }

    return hashlib.sha256(
        json.dumps(src, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def translate_chunk(
    chunk: list[SubtitleItem],
    global_context: str,
    primary_context_block: str,
    reference_block: str,
    source_lang: str,
    split_retry_depth: int = MAX_SPLIT_RETRY_DEPTH,
) -> dict[str, str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    start_idx = chunk[0].idx
    end_idx = chunk[-1].idx

    cache_key = chunk_cache_key(
        chunk=chunk,
        global_context=global_context,
        primary_context_block=primary_context_block,
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

    payload = items_to_target_json(chunk)
    primary_context_text = primary_context_block.strip() if primary_context_block.strip() else "(無)"
    ref_text = reference_block.strip() if reference_block.strip() else "(無)"

    expected_count = len(chunk)
    first_id = chunk[0].idx
    last_id = chunk[-1].idx

    prompt = f"""你是專業影音字幕翻譯員。

目標：
把「主要來源字幕」翻成自然的台灣繁體中文。

重要規則：
1. 目標字幕是 JSON array；每筆都有 id、timecode、source_text。
2. 必須逐筆翻譯 source_text：id "54" 只能翻譯 id "54" 的 source_text，不可拿前後句內容來填。
3. 不可合併、不可跳號、不可摘要、不可把第 N+1 筆內容挪到第 N 筆。
4. 這個片段必須輸出 exactly {expected_count} 筆 translations。
5. 第一筆 id 必須是 "{first_id}"，最後一筆 id 必須是 "{last_id}"。
6. 不要輸出時間軸。
7. 每個 id 只輸出翻譯後文字。
8. 用台灣自然口語，不要使用中國用語。
9. 不要逐字硬翻；可根據全片上下文、前後文 overlap、reference 字幕理解意思，但不可改變每個 id 對應的內容。
10. 「前後文 overlap」只供理解，不可輸出這裡的非目標內容。
11. 若自動字幕有重複、斷句錯、語助詞太多，請在同一個 id 內整理成順暢短句，但不可把內容移到其他 id。
12. 不要加註解、不要括號補充、不要說明。
13. 若某句是人名、笑聲、語氣詞，也要給合適的繁中字幕。
14. 若 reference 與主要來源衝突，以主要來源為主；reference 只用來輔助理解。

主要來源語言代碼：{source_lang}

請只翻譯下面這段「目標字幕」。每一個目標字幕 ID 都必須出現在 JSON translations 裡。
目標字幕：
{payload}

全片上下文筆記（只供理解，不可當作要翻譯的字幕）：
{global_context}

主要來源字幕前後文 overlap（只供理解，不可輸出這裡的非目標內容）：
{primary_context_text}

目前片段附近的其他語言 reference（只供理解，不可當作要翻譯的字幕）：
{ref_text}

輸出必須是符合 JSON schema 的物件：
{{
  "translations": [
    {{"id": "1", "text": "翻譯文字"}},
    {{"id": "2", "text": "翻譯文字"}}
  ]
}}
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

    translated_text = call_openai(
        messages,
        model=MODEL,
        temperature=0.2,
        response_format=translation_response_format(expected_count),
    )
    try:
        parsed_raw = parse_structured_translations(translated_text)
    except Exception as e:
        print(
            f"WARNING: structured JSON parse failed in chunk {start_idx}-{end_idx}, falling back to text parser: {e}",
            file=sys.stderr,
        )
        parsed_raw = parse_translated_numbered_text(translated_text)
    target_ids = {item.idx for item in chunk}
    parsed = {idx: text for idx, text in parsed_raw.items() if idx in target_ids}

    extra = sorted(set(parsed_raw) - target_ids, key=lambda x: int(x) if x.isdigit() else 0)
    if extra:
        print(
            f"WARNING: ignored non-target IDs in chunk {start_idx}-{end_idx}: "
            + ", ".join(extra[:20])
            + (" ..." if len(extra) > 20 else ""),
            file=sys.stderr,
        )

    missing = [item.idx for item in chunk if item.idx not in parsed]

    if missing:
        print(
            f"WARNING: missing translated IDs in chunk {start_idx}-{end_idx}: "
            + ", ".join(missing[:20])
            + (" ..." if len(missing) > 20 else ""),
            file=sys.stderr,
        )
        if split_retry_depth > 0 and len(chunk) >= 20:
            mid = len(chunk) // 2
            print(
                f"retry chunk {start_idx}-{end_idx} as halves: {chunk[0].idx}-{chunk[mid - 1].idx}, {chunk[mid].idx}-{chunk[-1].idx}",
                file=sys.stderr,
            )
            repaired = dict(parsed)
            for subchunk in (chunk[:mid], chunk[mid:]):
                repaired.update(
                    translate_chunk(
                        chunk=subchunk,
                        global_context=global_context,
                        primary_context_block=primary_context_block,
                        reference_block=reference_block,
                        source_lang=source_lang,
                        split_retry_depth=split_retry_depth - 1,
                    )
                )

            still_missing = [item for item in chunk if item.idx not in repaired]
            if 0 < len(still_missing) <= 5:
                print(
                    f"retry remaining singleton IDs in chunk {start_idx}-{end_idx}: "
                    + ", ".join(item.idx for item in still_missing),
                    file=sys.stderr,
                )
                repaired.update(
                    translate_chunk(
                        chunk=still_missing,
                        global_context=global_context,
                        primary_context_block=primary_context_block,
                        reference_block=reference_block,
                        source_lang=source_lang,
                        split_retry_depth=0,
                    )
                )
            return repaired
        return parsed

    cache_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return parsed


def translate_chunk_job(
    chunk_no: int,
    total_chunks: int,
    chunk: list[SubtitleItem],
    primary_items: list[SubtitleItem],
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

    context_items = context_items_for_chunk(primary_items, chunk, CHUNK_OVERLAP_SECONDS)
    primary_context_block = outside_chunk_context_text(context_items, chunk)
    reference_block = build_reference_block_for_chunk(reference_map, chunk)

    parsed = translate_chunk(
        chunk=chunk,
        global_context=global_context,
        primary_context_block=primary_context_block,
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
                primary_items=primary_items,
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
                        primary_items,
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
