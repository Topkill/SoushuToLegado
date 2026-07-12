#!/usr/bin/env python3
"""Merge imported readRecord.json into an existing Legado readRecord.json.

Design goals:
- Match records by exact bookName (deviceId is not the merge key).
- Keep existing records first; append import-only books after them.
- Same bookName:
    readTime  = existing.readTime + import.readTime
    lastRead  = existing.lastRead (never overwritten / never recomputed)
    deviceId  = existing.deviceId (import "" does not override)
- Import-only bookName:
    append with original readTime / lastRead
    deviceId = ""
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any


MEMBER_NAME = "readRecord.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge imported Legado readRecord.json into an existing one. "
            "Same bookName sums readTime and keeps existing lastRead/deviceId; "
            "import-only books are appended with original timestamps."
        )
    )
    parser.add_argument(
        "--existing",
        required=True,
        help="existing Legado backup: readRecord.json / directory / zip",
    )
    parser.add_argument(
        "--import",
        dest="import_path",
        required=True,
        help="imported backup to merge: readRecord.json / directory / zip",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="output readRecord.json path",
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
    return str(value).strip()


def normalize_item(raw: Any, *, source_label: str, index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        raise RuntimeError(f"{source_label}[{index}] 不是对象")

    book_name = as_text(raw.get("bookName"))
    if not book_name:
        return None

    read_time = as_int(raw.get("readTime"), "readTime", default=0)
    if read_time < 0:
        read_time = 0

    return {
        "bookName": book_name,
        "deviceId": as_text(raw.get("deviceId")),
        "readTime": read_time,
        "lastRead": as_int(raw.get("lastRead"), "lastRead", default=0),
    }


def prefer_existing_target(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick one existing row when multiple deviceIds share the same bookName."""
    # Prefer non-empty deviceId, then larger readTime, then newer lastRead.
    return max(
        candidates,
        key=lambda item: (
            1 if item["deviceId"] else 0,
            item["readTime"],
            item["lastRead"],
        ),
    )


def merge_read_records(
    existing_raw: list[Any],
    imported_raw: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Correct merge: aggregate import by bookName first, then fold into existing."""
    existing: list[dict[str, Any]] = []
    by_name: dict[str, list[dict[str, Any]]] = {}

    for index, raw in enumerate(existing_raw):
        item = normalize_item(raw, source_label="existing", index=index)
        if item is None:
            continue
        existing.append(item)
        by_name.setdefault(item["bookName"], []).append(item)

    # Aggregate import side by exact bookName.
    import_agg: dict[str, dict[str, int]] = {}
    skipped_empty = 0
    skipped_non_positive_rows = 0
    import_row_count = 0

    for index, raw in enumerate(imported_raw):
        item = normalize_item(raw, source_label="import", index=index)
        if item is None:
            skipped_empty += 1
            continue
        import_row_count += 1
        if item["readTime"] <= 0:
            skipped_non_positive_rows += 1
            continue

        name = item["bookName"]
        bucket = import_agg.get(name)
        if bucket is None:
            import_agg[name] = {
                "readTime": item["readTime"],
                "lastRead": item["lastRead"],
            }
        else:
            bucket["readTime"] += item["readTime"]
            if item["lastRead"] > bucket["lastRead"]:
                bucket["lastRead"] = item["lastRead"]

    appended: list[dict[str, Any]] = []
    merged_names: list[dict[str, Any]] = []

    # Preserve import encounter order for append-only books.
    ordered_import_names: list[str] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(imported_raw):
        item = normalize_item(raw, source_label="import", index=index)
        if item is None or item["readTime"] <= 0:
            continue
        if item["bookName"] in seen_names:
            continue
        seen_names.add(item["bookName"])
        ordered_import_names.append(item["bookName"])

    for book_name in ordered_import_names:
        imp = import_agg[book_name]
        candidates = by_name.get(book_name)
        if candidates:
            target = prefer_existing_target(candidates)
            before = target["readTime"]
            target["readTime"] = before + imp["readTime"]
            merged_names.append(
                {
                    "bookName": book_name,
                    "deviceId": target["deviceId"],
                    "existingReadTime": before,
                    "importReadTime": imp["readTime"],
                    "mergedReadTime": target["readTime"],
                    "lastReadKept": target["lastRead"],
                    "importLastReadIgnored": imp["lastRead"],
                }
            )
            continue

        new_item = {
            "bookName": book_name,
            "deviceId": "",
            "readTime": imp["readTime"],
            "lastRead": imp["lastRead"],
        }
        appended.append(new_item)
        by_name.setdefault(book_name, []).append(new_item)

    merged = existing + appended
    summary = {
        "existingCount": len(existing),
        "importRowsScanned": len(imported_raw),
        "importBooksAggregated": len(import_agg),
        "mergedSameNameCount": len(merged_names),
        "appendedCount": len(appended),
        "skippedEmptyCount": skipped_empty,
        "skippedNonPositiveRowCount": skipped_non_positive_rows,
        "mergedSameNames": merged_names,
        "appendedBooks": [
            {
                "bookName": item["bookName"],
                "readTime": item["readTime"],
                "lastRead": item["lastRead"],
            }
            for item in appended
        ],
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
    merged, summary = merge_read_records(existing_raw, imported_raw)

    write_json(output_path, merged)

    payload = {
        "existing": str(existing_path),
        "import": str(import_path),
        "output": str(output_path),
        "policy": {
            "matchBy": "bookName",
            "sameNameReadTime": "sum",
            "sameNameLastRead": "keep_existing",
            "sameNameDeviceId": "keep_existing",
            "importOnlyDeviceId": "",
            "importOnlyLastRead": "keep_import_original",
            "importOnlyReadTime": "keep_import_original",
            "outputOrder": "existing_then_appended",
        },
        **summary,
    }
    if mapping_out is not None:
        write_json(mapping_out, payload)

    print(f"existing records: {summary['existingCount']}")
    print(f"import books aggregated: {summary['importBooksAggregated']}")
    print(f"same-name merged: {summary['mergedSameNameCount']}")
    print(f"import-only appended: {summary['appendedCount']}")
    print(f"total after merge: {len(merged)}")
    print(f"output: {output_path}")
    if mapping_out is not None:
        print(f"mapping: {mapping_out}")

    if args.verbose:
        for item in summary["mergedSameNames"]:
            print(
                f"  = {item['bookName']}: "
                f"{item['existingReadTime']}+{item['importReadTime']}"
                f"={item['mergedReadTime']}, "
                f"lastRead kept={item['lastReadKept']}"
            )
        for item in summary["appendedBooks"]:
            print(
                f"  + {item['bookName']}: "
                f"readTime={item['readTime']}, lastRead={item['lastRead']}"
            )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
