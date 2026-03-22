#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PROMPT_ID = "cleanup-zh-v1-compact"
PLAN_VERSION = 1
CACHE_VERSION = 1
DEFAULT_CACHE_FILENAME = ".podcast-transcript-editor-cache.json"

SUSPICIOUS_MARKERS = (
    "[MUSIC]",
    "[BLANK_AUDIO]",
    "[NOISE]",
    "[UNK]",
    "[LAUGHTER]",
    "[APPLAUSE]",
)
END_PUNCT = "。！？!?…"
ALL_PUNCT = END_PUNCT + "，；：、,;:"
REPEATED_PHRASE_RE = re.compile(r"(.{2,12}?)(?:\1){2,}")
ASCII_WORD_RE = re.compile(r"[A-Za-z]{3,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def require_file(path_str: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def split_transcript_markdown(markdown: str) -> tuple[str, list[str]]:
    marker = "\n## 正文\n"
    if marker not in markdown:
        raise ValueError("transcript markdown missing '## 正文' section")
    head, body = markdown.split(marker, 1)
    header = head.strip() + "\n\n## 正文\n\n"
    blocks = [block.strip() for block in body.strip().split("\n\n") if block.strip()]
    return header, blocks


def join_body_text(block: str) -> str:
    lines = [line.rstrip() for line in block.splitlines()]
    if len(lines) <= 1:
        return ""
    return "".join(line.strip() for line in lines[1:] if line.strip())


def count_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def punctuation_density(text: str) -> float:
    chars = count_chars(text)
    if chars == 0:
        return 0.0
    punct = sum(1 for ch in text if ch in ALL_PUNCT)
    return punct / chars


def block_hash(block: str) -> str:
    return hashlib.sha256(block.encode("utf-8")).hexdigest()


def cache_key_for_block(block: str, prompt_id: str = PROMPT_ID) -> str:
    payload = f"{prompt_id}\n{block_hash(block)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_profile_context(path: Path | None) -> tuple[dict[str, str], list[str]]:
    if path is None:
        return {}, []
    raw = load_json(path)
    replacements = raw.get("replacements") if isinstance(raw.get("replacements"), dict) else {}
    noise_phrases = raw.get("noise_phrases") if isinstance(raw.get("noise_phrases"), list) else []
    safe_replacements = {str(k): str(v) for k, v in replacements.items() if isinstance(k, str) and isinstance(v, str)}
    safe_noise = [str(item) for item in noise_phrases if isinstance(item, str) and item.strip()]
    return safe_replacements, safe_noise


def default_cache_path(transcript_path: Path) -> Path:
    return transcript_path.parent / DEFAULT_CACHE_FILENAME


def load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {
            "version": CACHE_VERSION,
            "prompt_id": PROMPT_ID,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "entries": {},
        }
    raw = load_json(cache_path)
    if not isinstance(raw, dict):
        raise ValueError("cache file must contain a JSON object")
    if not isinstance(raw.get("entries"), dict):
        raw["entries"] = {}
    return raw


def save_cache(cache_path: Path, cache_data: dict[str, Any]) -> None:
    cache_data["version"] = CACHE_VERSION
    cache_data["updated_at"] = now_iso()
    cache_path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_header_for_output(header_text: str) -> str:
    rendered = header_text
    if rendered.startswith("# 转写稿\n"):
        rendered = rendered.replace("# 转写稿\n", "# 转写稿（忠实清洗版）\n", 1)
    if "- 清洗方式:" not in rendered and "- 识别语言:" in rendered:
        rendered = rendered.replace("- 识别语言:", "- 清洗方式: 忠实语义清洗\n- 识别语言:", 1)
    return rendered


def analyze_block(
    block: str,
    *,
    replacements: dict[str, str] | None = None,
    noise_phrases: list[str] | None = None,
) -> dict[str, Any]:
    replacements = replacements or {}
    noise_phrases = noise_phrases or []
    body = join_body_text(block)
    lines = block.splitlines()
    header_line = lines[0].strip() if lines else ""
    char_count = count_chars(body)
    end_punct_count = sum(1 for ch in body if ch in END_PUNCT)
    punct_density = punctuation_density(body)
    longest_line = max((len(line.strip()) for line in lines[1:] if line.strip()), default=0)
    ascii_words = ASCII_WORD_RE.findall(body)
    cjk_count = len(CJK_RE.findall(body))

    reasons: list[str] = []
    score = 0

    profile_hits = [old for old in replacements if old and old in body]
    if profile_hits:
        reasons.append("profile_replacement_hits")
        score += 2

    noise_hits = [phrase for phrase in noise_phrases if phrase and phrase in body]
    if noise_hits:
        reasons.append("noise_phrase_hits")
        score += 3

    if any(marker in body for marker in SUSPICIOUS_MARKERS):
        reasons.append("suspicious_marker")
        score += 3

    if REPEATED_PHRASE_RE.search(body):
        reasons.append("repeated_phrase")
        score += 2

    if char_count >= 140 and end_punct_count == 0:
        reasons.append("long_without_sentence_end")
        score += 2

    if char_count >= 260:
        reasons.append("very_long_block")
        score += 1

    if punct_density < 0.018 and char_count >= 120:
        reasons.append("sparse_punctuation")
        score += 1

    if longest_line > 120:
        reasons.append("long_line")
        score += 1

    if len(ascii_words) >= 8 and cjk_count >= 30:
        reasons.append("mixed_ascii_fragments")
        score += 1

    clean_short = (
        char_count <= 80
        and end_punct_count >= 1
        and punct_density >= 0.03
        and not reasons
    )
    clean_medium = (
        char_count <= 180
        and end_punct_count >= 1
        and punct_density >= 0.025
        and not reasons
    )

    if clean_short or clean_medium:
        decision = "pass_through"
        reasons = reasons or ["clean_enough"]
    elif score >= 2:
        decision = "needs_model"
    else:
        decision = "pass_through"
        reasons = reasons or ["clean_enough"]

    return {
        "header_line": header_line,
        "char_count": char_count,
        "end_punct_count": end_punct_count,
        "punctuation_density": round(punct_density, 4),
        "longest_line": longest_line,
        "dirty_score": score,
        "reasons": reasons,
        "decision": decision,
        "profile_replacement_hits": profile_hits,
        "noise_phrase_hits": noise_hits,
    }


def build_plan(
    transcript_path: Path,
    *,
    cache_path: Path | None = None,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    transcript_path = transcript_path.resolve()
    cache_path = (cache_path or default_cache_path(transcript_path)).resolve()
    replacements, noise_phrases = load_profile_context(profile_path.resolve() if profile_path else None)
    cache = load_cache(cache_path)

    markdown = transcript_path.read_text(encoding="utf-8")
    header_text, blocks = split_transcript_markdown(markdown)

    rendered_blocks: list[dict[str, Any]] = []
    stats = {"total_blocks": 0, "needs_model": 0, "pass_through": 0, "from_cache": 0}

    for index, block in enumerate(blocks, start=1):
        analysis = analyze_block(block, replacements=replacements, noise_phrases=noise_phrases)
        cache_key = cache_key_for_block(block)
        entry = cache.get("entries", {}).get(cache_key)
        cached_text = None
        decision = analysis["decision"]
        if isinstance(entry, dict) and entry.get("block_hash") == block_hash(block) and entry.get("prompt_id") == PROMPT_ID:
            cached_text = entry.get("cleaned_block") if isinstance(entry.get("cleaned_block"), str) else None
            if cached_text:
                decision = "from_cache"

        rendered = {
            "index": index,
            "cache_key": cache_key,
            "decision": decision,
            "dirty_score": analysis["dirty_score"],
            "reasons": analysis["reasons"],
            "char_count": analysis["char_count"],
            "header_line": analysis["header_line"],
            "source_block": block,
            "cleaned_block": cached_text,
            "profile_replacement_hits": analysis["profile_replacement_hits"],
            "noise_phrase_hits": analysis["noise_phrase_hits"],
        }
        rendered_blocks.append(rendered)
        stats["total_blocks"] += 1
        stats[decision] += 1

    return {
        "version": PLAN_VERSION,
        "created_at": now_iso(),
        "prompt_id": PROMPT_ID,
        "transcript_path": str(transcript_path),
        "cache_path": str(cache_path),
        "profile_path": str(profile_path.resolve()) if profile_path else None,
        "header_text": header_text,
        "stats": stats,
        "blocks": rendered_blocks,
    }


def write_plan(plan: dict[str, Any], output_path: Path | None) -> None:
    rendered = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if output_path is None:
        print(rendered, end="")
        return
    output_path.write_text(rendered, encoding="utf-8")


def assemble_from_plan(plan_path: Path, output_path: Path, *, model: str | None = None) -> Path:
    plan = load_json(plan_path)
    blocks = plan.get("blocks") if isinstance(plan.get("blocks"), list) else []
    header_text = plan.get("header_text") if isinstance(plan.get("header_text"), str) else ""
    cache_path = Path(plan.get("cache_path")) if isinstance(plan.get("cache_path"), str) else output_path.parent / DEFAULT_CACHE_FILENAME
    cache = load_cache(cache_path)

    rendered_blocks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        decision = block.get("decision")
        source_block = block.get("source_block") if isinstance(block.get("source_block"), str) else ""
        cleaned_block = block.get("cleaned_block") if isinstance(block.get("cleaned_block"), str) else None
        cache_key = block.get("cache_key") if isinstance(block.get("cache_key"), str) else ""
        if decision == "pass_through":
            rendered_blocks.append(source_block)
            continue
        if decision in {"from_cache", "needs_model"}:
            if not cleaned_block:
                raise ValueError(f"missing cleaned_block for plan block {block.get('index')}")
            rendered_blocks.append(cleaned_block)
            cache.setdefault("entries", {})[cache_key] = {
                "block_hash": block_hash(source_block),
                "prompt_id": PROMPT_ID,
                "cleaned_block": cleaned_block,
                "source_header": block.get("header_line"),
                "model": model,
                "updated_at": now_iso(),
            }
            continue
        raise ValueError(f"unknown decision: {decision}")

    output_text = clean_header_for_output(header_text) + "\n\n".join(rendered_blocks).strip() + "\n"
    output_path.write_text(output_text, encoding="utf-8")
    save_cache(cache_path, cache)
    return output_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Helper utilities for the podcast-transcript-editor skill")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Analyze transcript blocks and produce a cleanup plan")
    plan_parser.add_argument("transcript", help="Path to 01_transcript.md")
    plan_parser.add_argument("--cache-file", default=None, help="Optional cache file path")
    plan_parser.add_argument("--profile-file", default=None, help="Optional podcast profile JSON path")
    plan_parser.add_argument("--output", default=None, help="Optional plan JSON output path")

    assemble_parser = subparsers.add_parser("assemble", help="Assemble 02_transcript_clean.md from a completed plan")
    assemble_parser.add_argument("plan", help="Path to cleanup plan JSON")
    assemble_parser.add_argument("--output", required=True, help="Output path for 02_transcript_clean.md")
    assemble_parser.add_argument("--model", default=None, help="Model name to record in cache entries")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "plan":
        transcript_path = require_file(args.transcript)
        cache_path = Path(args.cache_file).resolve() if args.cache_file else None
        profile_path = require_file(args.profile_file).resolve() if args.profile_file else None
        output_path = Path(args.output).resolve() if args.output else None
        plan = build_plan(transcript_path, cache_path=cache_path, profile_path=profile_path)
        write_plan(plan, output_path)
        return 0

    if args.command == "assemble":
        plan_path = require_file(args.plan)
        output_path = Path(args.output).resolve()
        assemble_from_plan(plan_path, output_path, model=args.model)
        return 0

    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
