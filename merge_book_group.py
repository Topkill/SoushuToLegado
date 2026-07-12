#!/usr/bin/env python3
"""Merge imported bookGroup.json into an existing Legado bookGroup.json.

Design goals:
- Keep every existing group as-is (no overwrite by groupId/groupName).
- Append imported custom groups (groupId > 0) after existing custom groups.
- Reallocate unused positive bit groupIds for imported groups.
- Assign order values after current custom maxOrder.
- Optional name conflict handling for duplicate groupName values.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any


GROUP_NAME_CONFLICT_KEEP = "keep"
GROUP_NAME_CONFLICT_NUMBER = "number"
GROUP_NAME_CONFLICT_CHOICES = (GROUP_NAME_CONFLICT_KEEP, GROUP_NAME_CONFLICT_NUMBER)

DEFAULT_GROUP_FIELDS = {
    "bookSort": -1,
    "enableRefresh": True,
    "show": True,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge imported Legado bookGroup.json into an existing one. "
            "Existing groups are preserved; imported custom groups are appended."
        )
    )
    parser.add_argument(
        "--existing",
        required=True,
        help="existing Legado backup: bookGroup.json / directory / zip",
    )
    parser.add_argument(
        "--import",
        dest="import_path",
        required=True,
        help="imported backup to merge: bookGroup.json / directory / zip",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="output bookGroup.json path",
    )
    parser.add_argument(
        "--mapping-out",
        help="optional path for old->new groupId mapping JSON",
    )
    parser.add_argument(
        "--group-name-conflict",
        choices=GROUP_NAME_CONFLICT_CHOICES,
        default=GROUP_NAME_CONFLICT_KEEP,
        help=(
            "how to name imported groups when groupName already exists: "
            "keep=allow duplicate names (default); "
            "number=rename conflicts to 名称（1）, 名称（2）, ..."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print merge details",
    )
    return parser.parse_args()


def load_json_array(path: Path, member_name: str = "bookGroup.json") -> list[Any]:
    """Load a JSON array from a bare json file, directory, or zip backup."""
    if not path.exists():
        raise RuntimeError(f"路径不存在：{path}")

    if path.is_dir():
        json_path = path / member_name
        if not json_path.is_file():
            raise RuntimeError(f"目录中未找到 {member_name}：{path}")
        data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    elif path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            names = {name.replace("\\", "/"): name for name in zf.namelist()}
            # Prefer root-level member, then any nested same basename.
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
                raise RuntimeError(f"zip 中未找到 {member_name}：{path}")
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


def normalize_group(raw: Any, *, source_label: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"{source_label}[{index}] 不是对象")

    group_id = as_int(raw.get("groupId"), "groupId")
    group_name = as_text(raw.get("groupName"))
    if not group_name and group_id > 0:
        raise RuntimeError(f"{source_label}[{index}] 自定义分组缺少 groupName")

    group: dict[str, Any] = {
        "groupId": group_id,
        "groupName": group_name,
        "order": as_int(raw.get("order"), "order", default=0),
        "enableRefresh": bool(raw.get("enableRefresh", DEFAULT_GROUP_FIELDS["enableRefresh"])),
        "show": bool(raw.get("show", DEFAULT_GROUP_FIELDS["show"])),
        "bookSort": as_int(raw.get("bookSort"), "bookSort", default=DEFAULT_GROUP_FIELDS["bookSort"]),
    }
    if "cover" in raw:
        group["cover"] = raw.get("cover")
    if "onlyUpdateRead" in raw:
        group["onlyUpdateRead"] = bool(raw.get("onlyUpdateRead"))
    return group


def is_custom_group(group: dict[str, Any]) -> bool:
    return as_int(group["groupId"], "groupId") > 0


def is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def collect_used_bits(groups: list[dict[str, Any]]) -> int:
    used = 0
    for group in groups:
        group_id = as_int(group["groupId"], "groupId")
        if group_id > 0:
            used |= group_id
    return used


def next_unused_bit(used_bits: int) -> int:
    bit = 1
    # Legado custom groups use positive bit flags; practical upper bound is 63 bits.
    for _ in range(63):
        if used_bits & bit == 0:
            return bit
        bit <<= 1
    raise RuntimeError("自定义分组 bit 已用尽（最多约 63 个正数 groupId 位）")


def next_appended_order(groups: list[dict[str, Any]]) -> int:
    """First order for newly appended custom groups.

    Mirrors Legado maxOrder over groupId >= 0:
    - only builtin groups (all groupId < 0) or empty list -> start at 1
    - existing custom/non-negative groups -> max(order) + 1

    Builtin negative orders are intentionally ignored so imported shelves
    always land after custom shelves, not mixed into builtin order space.
    """
    orders = [
        as_int(group["order"], "order", default=0)
        for group in groups
        if as_int(group["groupId"], "groupId") >= 0
    ]
    if not orders:
        return 1
    return max(orders) + 1


def existing_custom_names(groups: list[dict[str, Any]]) -> set[str]:
    """Names used by custom groups only.

    Builtin labels like 本地/全部 are normal system names and should not force
    number-mode renames of imported custom shelves.
    """
    names: set[str] = set()
    for group in groups:
        if not is_custom_group(group):
            continue
        name = as_text(group.get("groupName"))
        if name:
            names.add(name)
    return names


def resolve_group_name(
    desired_name: str,
    taken_names: set[str],
    conflict_mode: str,
) -> str:
    name = desired_name.strip()
    if not name:
        raise RuntimeError("导入自定义分组的 groupName 不能为空")

    if conflict_mode == GROUP_NAME_CONFLICT_KEEP:
        return name

    if conflict_mode != GROUP_NAME_CONFLICT_NUMBER:
        raise RuntimeError(f"未知 group-name-conflict：{conflict_mode}")

    if name not in taken_names:
        return name

    index = 1
    while True:
        candidate = f"{name}（{index}）"
        if candidate not in taken_names:
            return candidate
        index += 1


def merge_book_groups(
    existing_raw: list[Any],
    imported_raw: list[Any],
    *,
    conflict_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing = [
        normalize_group(item, source_label="existing", index=i)
        for i, item in enumerate(existing_raw)
    ]
    imported = [
        normalize_group(item, source_label="import", index=i)
        for i, item in enumerate(imported_raw)
    ]

    # Preserve existing order and content completely.
    merged: list[dict[str, Any]] = [dict(group) for group in existing]
    used_bits = collect_used_bits(merged)
    next_order = next_appended_order(merged)
    taken = existing_custom_names(merged)

    mapping: list[dict[str, Any]] = []
    seen_import_ids: set[int] = set()

    for group in imported:
        if not is_custom_group(group):
            # Builtin / non-custom groups always stay from existing side.
            continue

        old_group_id = as_int(group["groupId"], "groupId")
        if old_group_id in seen_import_ids:
            # Defensive: skip duplicate ids in import file.
            continue
        seen_import_ids.add(old_group_id)

        # Always allocate a fresh unused power-of-two bit.
        # Import side ids may collide with existing ids; never reuse them as-is.
        new_group_id = next_unused_bit(used_bits)
        used_bits |= new_group_id

        old_name = as_text(group["groupName"])
        new_name = resolve_group_name(old_name, taken, conflict_mode)
        taken.add(new_name)

        new_group = dict(group)
        new_group["groupId"] = new_group_id
        new_group["groupName"] = new_name
        new_group["order"] = next_order
        next_order += 1
        merged.append(new_group)

        mapping.append(
            {
                "oldGroupId": old_group_id,
                "oldGroupName": old_name,
                "newGroupId": new_group_id,
                "newGroupName": new_name,
                "order": new_group["order"],
                "nameConflict": old_name != new_name,
            }
        )

    return merged, mapping


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
    merged, mapping = merge_book_groups(
        existing_raw,
        imported_raw,
        conflict_mode=args.group_name_conflict,
    )

    write_json(output_path, merged)

    mapping_payload = {
        "groupNameConflict": args.group_name_conflict,
        "existing": str(existing_path),
        "import": str(import_path),
        "output": str(output_path),
        "appendedCount": len(mapping),
        "groupIdMap": {
            str(item["oldGroupId"]): item["newGroupId"] for item in mapping
        },
        "groups": mapping,
    }
    if mapping_out is not None:
        write_json(mapping_out, mapping_payload)

    custom_existing = sum(1 for g in merged if is_custom_group(g)) - len(mapping)
    print(f"existing groups: {len(existing_raw)}")
    print(f"imported custom groups appended: {len(mapping)}")
    print(f"custom groups after merge: {custom_existing + len(mapping)}")
    print(f"total groups after merge: {len(merged)}")
    print(f"group-name-conflict: {args.group_name_conflict}")
    print(f"output: {output_path}")
    if mapping_out is not None:
        print(f"mapping: {mapping_out}")

    if args.verbose:
        for item in mapping:
            rename = ""
            if item["nameConflict"]:
                rename = f"  (renamed from {item['oldGroupName']})"
            print(
                f"  + {item['oldGroupName']}: "
                f"{item['oldGroupId']} -> {item['newGroupId']}, "
                f"order={item['order']}, name={item['newGroupName']}{rename}"
            )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
