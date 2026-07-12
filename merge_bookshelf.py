#!/usr/bin/env python3
"""Merge imported bookshelf.json into an existing Legado bookshelf.json.

Design goals:
- Keep every existing book as-is.
- Append import-only books after existing ones.
- Remap import group bits with bookGroup merge mapping (final groupIds).
- Same name+author as an existing book: skip import (protect Legado).
- Reassign appended books order = min(existing.order) - 1, -2, ... (Legado add-book style).
- Report skipped duplicates so the user can see what was not appended.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any


MEMBER_NAME = "bookshelf.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge imported Legado bookshelf.json into an existing one. "
            "Existing books are preserved; import books with the same name+author "
            "are skipped; other import books are remapped by group and appended."
        )
    )
    parser.add_argument(
        "--existing",
        required=True,
        help="existing Legado backup: bookshelf.json / directory / zip",
    )
    parser.add_argument(
        "--import",
        dest="import_path",
        required=True,
        help="imported backup to merge: bookshelf.json / directory / zip",
    )
    parser.add_argument(
        "--group-mapping",
        required=True,
        help="mapping json from merge_book_group.py (--mapping-out)",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="output bookshelf.json path",
    )
    parser.add_argument(
        "--mapping-out",
        help="optional path for merge summary JSON",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print per-book skip/append details",
    )
    return parser.parse_args()


def load_json_array(path: Path, member_name: str = MEMBER_NAME) -> list[Any]:
    """Load a JSON array from a bare json file, directory, or zip backup."""
    if not path.exists():
        raise RuntimeError(f"路径不存在：{path}")

    if path.is_dir():
        json_path = path / member_name
        if not json_path.is_file():
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


def load_group_id_map(path: Path) -> dict[int, int]:
    if not path.exists():
        raise RuntimeError(f"group mapping 不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(f"group mapping 顶层必须是对象：{path}")

    raw_map = data.get("groupIdMap")
    if raw_map is None and "groups" in data and isinstance(data["groups"], list):
        raw_map = {
            str(item["oldGroupId"]): item["newGroupId"]
            for item in data["groups"]
            if isinstance(item, dict) and "oldGroupId" in item and "newGroupId" in item
        }
    if raw_map is None:
        # Allow a plain {old: new} object.
        raw_map = {
            key: value
            for key, value in data.items()
            if str(key).lstrip("-").isdigit()
        }

    if not isinstance(raw_map, dict):
        raise RuntimeError(f"group mapping 缺少 groupIdMap：{path}")

    result: dict[int, int] = {}
    for old_key, new_val in raw_map.items():
        try:
            old_id = int(old_key)
            new_id = int(new_val)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"非法 groupId 映射：{old_key!r} -> {new_val!r}") from exc
        if old_id <= 0 or new_id <= 0:
            continue
        result[old_id] = new_id
    return result


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
    return str(value).strip()


def name_author_key(name: str, author: str) -> tuple[str, str]:
    # Exact match after strip only; case / fullwidth differences are different books.
    return (name, author)


def extract_positive_bits(group_value: int) -> list[int]:
    """Return individual power-of-two bits set in a positive group bitmask."""
    if group_value <= 0:
        return []
    bits: list[int] = []
    bit = 1
    while bit <= group_value and bit > 0:
        if group_value & bit:
            bits.append(bit)
        bit <<= 1
    return bits



def min_existing_order(books: list[dict[str, Any]]) -> int:
    """Mirror Legado bookDao.minOrder for reassignment of appended books.

    If there are no existing books or no usable order values, start from 0 so the
    first appended book becomes -1.
    """
    orders: list[int] = []
    for book in books:
        if "order" not in book or book.get("order") is None:
            continue
        orders.append(as_int(book.get("order"), "order", default=0))
    if not orders:
        return 0
    return min(orders)


def remap_group(group_value: int, group_id_map: dict[int, int]) -> int:
    """Remap custom positive bits; leave non-positive values unchanged."""
    if group_value <= 0:
        return group_value

    new_group = 0
    for old_bit in extract_positive_bits(group_value):
        new_bit = group_id_map.get(old_bit)
        if new_bit is None:
            # Unknown custom bit from import: drop it rather than collide.
            continue
        new_group |= new_bit
    return new_group


def merge_bookshelf(
    existing_raw: list[Any],
    imported_raw: list[Any],
    group_id_map: dict[int, int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    seen_name_author: set[tuple[str, str]] = set()

    for index, raw in enumerate(existing_raw):
        if not isinstance(raw, dict):
            raise RuntimeError(f"existing[{index}] 不是对象")
        book = dict(raw)
        existing.append(book)
        key = name_author_key(as_text(book.get("name")), as_text(book.get("author")))
        # Empty name still participates; Legado unique index is (name, author).
        seen_name_author.add(key)

    appended: list[dict[str, Any]] = []
    skipped_duplicates: list[dict[str, Any]] = []
    remapped_groups: list[dict[str, Any]] = []

    for index, raw in enumerate(imported_raw):
        if not isinstance(raw, dict):
            raise RuntimeError(f"import[{index}] 不是对象")

        book = dict(raw)
        name = as_text(book.get("name"))
        author = as_text(book.get("author"))
        key = name_author_key(name, author)
        old_group = as_int(book.get("group"), "group", default=0)
        new_group = remap_group(old_group, group_id_map)

        if key in seen_name_author:
            skipped_duplicates.append(
                {
                    "name": name,
                    "author": author,
                    "importBookUrl": as_text(book.get("bookUrl")),
                    "importOrigin": as_text(book.get("origin")),
                    "oldGroup": old_group,
                    "remappedGroup": new_group,
                    "reason": "same name+author already exists in existing bookshelf",
                }
            )
            continue

        if new_group != old_group:
            remapped_groups.append(
                {
                    "name": name,
                    "author": author,
                    "oldGroup": old_group,
                    "newGroup": new_group,
                }
            )
        book["group"] = new_group
        # Placeholder; final order assigned after all skips are known, in append order.
        appended.append(book)
        seen_name_author.add(key)

    # Match Legado LocalBook: new book order = minOrder - 1, then continue decreasing.
    next_order = min_existing_order(existing) - 1
    for book in appended:
        old_order = book.get("order")
        book["order"] = next_order
        next_order -= 1

    merged = existing + appended
    summary = {
        "existingCount": len(existing),
        "importCount": len(imported_raw),
        "appendedCount": len(appended),
        "skippedDuplicateCount": len(skipped_duplicates),
        "groupBitsRemappedCount": len(remapped_groups),
        "skippedDuplicates": skipped_duplicates,
        "appendedBooks": [
            {
                "name": as_text(book.get("name")),
                "author": as_text(book.get("author")),
                "bookUrl": as_text(book.get("bookUrl")),
                "group": as_int(book.get("group"), "group", default=0),
                "order": as_int(book.get("order"), "order", default=0),
            }
            for book in appended
        ],
        "remappedGroups": remapped_groups,
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
    group_mapping_path = Path(args.group_mapping)
    output_path = Path(args.output)
    mapping_out = Path(args.mapping_out) if args.mapping_out else None

    existing_raw = load_json_array(existing_path)
    imported_raw = load_json_array(import_path)
    group_id_map = load_group_id_map(group_mapping_path)

    merged, summary = merge_bookshelf(existing_raw, imported_raw, group_id_map)
    write_json(output_path, merged)

    payload = {
        "existing": str(existing_path),
        "import": str(import_path),
        "groupMapping": str(group_mapping_path),
        "output": str(output_path),
        "policy": {
            "existingBooks": "keep",
            "outputOrder": "existing_then_appended",
            "duplicateMatch": "exact name+author",
            "duplicateAction": "skip_import_keep_existing",
            "importGroup": "remap via groupIdMap final groupIds",
            "appendedOrder": "min(existing.order)-1, -2, ...",
            "fieldMerge": "none",
        },
        "groupIdMap": {str(k): v for k, v in sorted(group_id_map.items())},
        **summary,
    }
    if mapping_out is not None:
        write_json(mapping_out, payload)

    print(f"existing books: {summary['existingCount']}")
    print(f"import books scanned: {summary['importCount']}")
    print(f"appended: {summary['appendedCount']}")
    print(f"skipped duplicates (same name+author): {summary['skippedDuplicateCount']}")
    print(f"total after merge: {len(merged)}")
    print(f"output: {output_path}")
    if mapping_out is not None:
        print(f"mapping: {mapping_out}")

    if summary["skippedDuplicateCount"]:
        print("duplicate books skipped (kept existing Legado):")
        for item in summary["skippedDuplicates"]:
            author = item["author"] or "(empty author)"
            print(f"  - {item['name']} / {author}")

    if args.verbose:
        for item in summary["appendedBooks"]:
            author = item["author"] or "(empty author)"
            print(
                f"  + {item['name']} / {author} "
                f"group={item['group']} order={item['order']} url={item['bookUrl']}"
            )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
