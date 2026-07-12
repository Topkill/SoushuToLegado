#!/usr/bin/env python3
"""Merge imported searchHistory.json into an existing Legado searchHistory.json.

Design goals:
- Keep every existing keyword record as-is (usage/lastUseTime untouched).
- Import side only contributes real words + relative order.
- Exact word match only: different case / fullwidth / halfwidth are different words.
- Duplicate words from import are skipped (existing wins).
- New import words are appended with synthetic lastUseTime after existing history,
  preserving the import list order (earlier in import list = slightly newer).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any


MEMBER_NAME = "searchHistory.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge imported Legado searchHistory.json into an existing one. "
            "Existing keywords are preserved; new import words are appended after them."
        )
    )
    parser.add_argument(
        "--existing",
        required=True,
        help="existing Legado backup: searchHistory.json / directory / zip",
    )
    parser.add_argument(
        "--import",
        dest="import_path",
        required=True,
        help="imported backup to merge: searchHistory.json / directory / zip",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="output searchHistory.json path",
    )
    parser.add_argument(
        "--mapping-out",
        help="optional path for merge summary JSON",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print merge details",
    )
    return parser.parse_args()


def load_json_array(path: Path, member_name: str = MEMBER_NAME) -> list[Any]:
    """Load a JSON array from a bare json file, directory, or zip backup."""
    if not path.exists():
        raise RuntimeError(f"路径不存在：{path}")

    if path.is_dir():
        json_path = path / member_name
        if not json_path.is_file():
            # Missing optional member -> empty list is valid for merge.
            return []
        data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    elif path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            names = {name.replace("\\", "/"): name for name in zf.namelist()}
            candidate = None
            if member_name in names:
                candidate = names[member_name]
            else:
                basenames = [
                    stored
                    for stored in zf.namelist()
                    if Path(stored.replace("\\", "/")).name == member_name
                ]
                if len(basenames) == 1:
                    candidate = basenames[0]
                elif len(basenames) > 1:
                    raise RuntimeError(
                        f"zip 中存在多个 {member_name}，请先解压后指定明确文件：{path}"
                    )
            if candidate is None:
                return []
            data = json.loads(zf.read(candidate).decode("utf-8-sig"))
    else:
        data = json.loads(path.read_text(encoding="utf-8-sig"))

    if not isinstance(data, list):
        raise RuntimeError(f"{member_name} 顶层必须是 JSON 数组：{path}")
    return data


def as_int(value: Any, field_name: str, *, default: int | None = None) -> int:
    if value is None:
        if default is not None:
            return default
        raise RuntimeError(f"缺少字段：{field_name}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"字段 {field_name} 不是整数：{value!r}") from exc


def as_text(value: Any) -> str:
    if value is None:
        return ""
    # Exact match key: only strip ends; do NOT casefold / NFKC / fullwidth normalize.
    return str(value).strip()


def normalize_item(raw: Any, *, source_label: str, index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        raise RuntimeError(f"{source_label}[{index}] 不是对象")

    word = as_text(raw.get("word"))
    if not word:
        return None

    return {
        "word": word,
        "usage": max(1, as_int(raw.get("usage"), "usage", default=1)),
        "lastUseTime": as_int(
            raw.get("lastUseTime"),
            "lastUseTime",
            default=int(time.time() * 1000),
        ),
    }


def min_last_use_time(items: list[dict[str, Any]], *, fallback: int) -> int:
    if not items:
        return fallback
    return min(as_int(item["lastUseTime"], "lastUseTime") for item in items)


def merge_search_history(
    existing_raw: list[Any],
    imported_raw: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    seen_words: set[str] = set()

    for index, raw in enumerate(existing_raw):
        item = normalize_item(raw, source_label="existing", index=index)
        if item is None:
            continue
        word = item["word"]
        if word in seen_words:
            # Keep first existing record for a word; later duplicates ignored.
            continue
        seen_words.add(word)
        existing.append(item)

    now_ms = int(time.time() * 1000)
    # Place import words strictly older than every existing timestamp so that
    # time-desc sort keeps existing history in front of imported words.
    next_time = min_last_use_time(existing, fallback=now_ms) - 1

    appended: list[dict[str, Any]] = []
    skipped_duplicates: list[str] = []
    skipped_empty = 0
    seen_import_words: set[str] = set()

    for index, raw in enumerate(imported_raw):
        item = normalize_item(raw, source_label="import", index=index)
        if item is None:
            skipped_empty += 1
            continue

        word = item["word"]
        if word in seen_import_words:
            continue
        seen_import_words.add(word)

        if word in seen_words:
            skipped_duplicates.append(word)
            continue

        # Preserve import relative order: first import word is newest among appended.
        new_item = {
            "word": word,
            "usage": 1,
            "lastUseTime": next_time,
        }
        next_time -= 1
        seen_words.add(word)
        appended.append(new_item)

    merged = existing + appended
    summary = {
        "existingCount": len(existing),
        "importCount": len(imported_raw),
        "appendedCount": len(appended),
        "skippedDuplicateCount": len(skipped_duplicates),
        "skippedEmptyCount": skipped_empty,
        "appendedWords": [item["word"] for item in appended],
        "skippedDuplicateWords": skipped_duplicates,
    }
    return merged, summary


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    existing_path = Path(args.existing)
    import_path = Path(args.import_path)
    output_path = Path(args.output)
    mapping_out = Path(args.mapping_out) if args.mapping_out else None

    existing_raw = load_json_array(existing_path)
    imported_raw = load_json_array(import_path)
    merged, summary = merge_search_history(existing_raw, imported_raw)

    write_json(output_path, merged)

    payload = {
        "existing": str(existing_path),
        "import": str(import_path),
        "output": str(output_path),
        **summary,
    }
    if mapping_out is not None:
        write_json(mapping_out, payload)

    print(f"existing keywords: {summary['existingCount']}")
    print(f"imported records scanned: {summary['importCount']}")
    print(f"appended new keywords: {summary['appendedCount']}")
    print(f"skipped exact duplicates: {summary['skippedDuplicateCount']}")
    print(f"total after merge: {len(merged)}")
    print(f"output: {output_path}")
    if mapping_out is not None:
        print(f"mapping: {mapping_out}")

    if args.verbose:
        for word in summary["appendedWords"]:
            print(f"  + {word}")
        for word in summary["skippedDuplicateWords"]:
            print(f"  = skip existing: {word}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
