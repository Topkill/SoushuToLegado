#!/usr/bin/env python3
"""Orchestrate Legado backup merge: existing Legado + converted soushu backup.

Pipeline (scheme A: import functions from per-json merge modules):
  1. bookGroup
  2. bookshelf  (needs groupId mapping)
  3. searchHistory
  4. readRecord
  5. pack zip = merged four JSON files + all other files from existing backup
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from merge_book_group import merge_book_groups
from merge_bookshelf import merge_bookshelf
from merge_read_record import merge_read_records
from merge_search_history import merge_search_history


CORE_MEMBERS = (
    "bookGroup.json",
    "bookshelf.json",
    "searchHistory.json",
    "readRecord.json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge a converted soushu/Legado-lite backup into an existing Legado backup. "
            "Existing data is the base; import bookshelves are appended with remapped groups."
        )
    )
    parser.add_argument(
        "--existing",
        required=True,
        help="existing Legado backup zip or extracted directory",
    )
    parser.add_argument(
        "--import",
        dest="import_path",
        required=True,
        help="import backup zip/dir (usually convert_bookshelf_backup.py output)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="legado-merged.zip",
        help="output merged Legado backup zip path (default: legado-merged.zip)",
    )
    parser.add_argument(
        "--group-name-conflict",
        choices=("keep", "number"),
        default="keep",
        help="bookGroup name conflict policy (default: keep)",
    )
    parser.add_argument(
        "--report",
        help="optional markdown report path",
    )
    parser.add_argument(
        "--work-dir",
        help="optional work directory (default: system temp)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="keep intermediate JSON/mapping files under work-dir",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print detailed merge lines",
    )
    return parser.parse_args()


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"{label} 不存在：{path}")


def list_backup_files(path: Path) -> dict[str, Path | str]:
    """Map basename -> source for reading bytes.

    Values are either a filesystem Path (directory backup) or a zip member name (zip backup).
    """
    if path.is_dir():
        result: dict[str, Path | str] = {}
        for child in path.iterdir():
            if child.is_file():
                result[child.name] = child
        return result
    if path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
        result = {}
        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                base = Path(name.replace("\\", "/")).name
                # Prefer root-level entries when duplicates exist.
                if base in result and "/" in name.replace("\\", "/").strip("/"):
                    continue
                result[base] = name
        return result
    raise RuntimeError(f"既不是目录也不是 zip：{path}")


def read_backup_member(path: Path, member_name: str) -> bytes | None:
    if path.is_dir():
        file_path = path / member_name
        if not file_path.is_file():
            return None
        return file_path.read_bytes()
    if path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            names = {name.replace("\\", "/"): name for name in zf.namelist()}
            if member_name in names:
                return zf.read(names[member_name])
            matches = [
                stored
                for stored in zf.namelist()
                if Path(stored.replace("\\", "/")).name == member_name
            ]
            if len(matches) == 1:
                return zf.read(matches[0])
            if len(matches) > 1:
                raise RuntimeError(f"zip 中存在多个 {member_name}：{path}")
            return None
    raise RuntimeError(f"无法读取：{path}")


def load_json_array_optional(path: Path, member_name: str) -> list[Any]:
    raw = read_backup_member(path, member_name)
    if raw is None:
        return []
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, list):
        raise RuntimeError(f"{member_name} 顶层必须是 JSON 数组：{path}")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_report(
    existing_path: Path,
    import_path: Path,
    output_path: Path,
    group_mode: str,
    group_summary: dict[str, Any],
    bookshelf_summary: dict[str, Any],
    search_summary: dict[str, Any],
    read_summary: dict[str, Any],
    passthrough_count: int,
) -> str:
    lines = [
        "# Legado 备份合并报告",
        "",
        f"- 现有备份：`{existing_path}`",
        f"- 导入备份：`{import_path}`",
        f"- 输出备份：`{output_path}`",
        f"- 分组同名策略：`{group_mode}`",
        f"- 原样透传文件数：{passthrough_count}",
        "",
        "## bookGroup",
        "",
        f"- 现有分组数：{group_summary.get('existingCount', 'n/a')}",
        f"- 追加自定义分组：{group_summary.get('appendedCount', 0)}",
        f"- 合并后总分组：{group_summary.get('totalCount', 'n/a')}",
        "",
    ]
    for item in group_summary.get("groups", []):
        rename = f" → {item['newGroupName']}" if item.get("nameConflict") else ""
        lines.append(
            f"- `{item['oldGroupName']}`: {item['oldGroupId']} → {item['newGroupId']}, "
            f"order={item['order']}{rename}"
        )

    lines.extend(
        [
            "",
            "## bookshelf",
            "",
            f"- 现有书籍：{bookshelf_summary.get('existingCount', 0)}",
            f"- 导入扫描：{bookshelf_summary.get('importCount', 0)}",
            f"- 追加：{bookshelf_summary.get('appendedCount', 0)}",
            f"- 跳过同名同作者：{bookshelf_summary.get('skippedDuplicateCount', 0)}",
            "",
        ]
    )
    for item in bookshelf_summary.get("skippedDuplicates", []):
        author = item.get("author") or "(empty author)"
        lines.append(f"- skip `{item.get('name')}` / {author}")
    for item in bookshelf_summary.get("appendedBooks", []):
        author = item.get("author") or "(empty author)"
        lines.append(
            f"- add `{item.get('name')}` / {author} "
            f"group={item.get('group')} order={item.get('order')}"
        )

    lines.extend(
        [
            "",
            "## searchHistory",
            "",
            f"- 现有关键词：{search_summary.get('existingCount', 0)}",
            f"- 追加：{search_summary.get('appendedCount', 0)}",
            f"- 跳过已存在词：{search_summary.get('skippedDuplicateCount', 0)}",
            "",
            "## readRecord",
            "",
            f"- 现有记录：{read_summary.get('existingCount', 0)}",
            f"- 同名累加：{read_summary.get('mergedSameNameCount', 0)}",
            f"- 仅导入追加：{read_summary.get('appendedCount', 0)}",
            "",
            "## 注意",
            "",
            "- 本地书只迁移书架记录，不迁移文件本身。",
            "- 搜书网文书通常没有可用 Legado 书源，可能需要换源。",
            "- 同名同作者导入书会跳过，以现有 Legado 为准。",
            "",
        ]
    )
    return "\n".join(lines)


def pack_output_zip(
    output_path: Path,
    existing_path: Path,
    merged_members: dict[str, Any],
) -> int:
    """Write output zip. Returns count of passthrough (non-core) files from existing."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_files = list_backup_files(existing_path)
    passthrough = 0

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Core merged JSON first for readability.
        for name in CORE_MEMBERS:
            payload = merged_members[name]
            zf.writestr(
                name,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

        for base_name, source in sorted(existing_files.items()):
            if base_name in CORE_MEMBERS:
                continue
            if isinstance(source, Path):
                data = source.read_bytes()
            else:
                data = read_backup_member(existing_path, base_name)
                if data is None:
                    continue
            zf.writestr(base_name, data)
            passthrough += 1

    return passthrough


def main() -> int:
    args = parse_args()
    existing_path = Path(args.existing)
    import_path = Path(args.import_path)
    output_path = Path(args.output)

    ensure_exists(existing_path, "existing")
    ensure_exists(import_path, "import")

    temp_root: Path | None = None
    work_dir: Path
    if args.work_dir:
        work_dir = Path(args.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="merge_legado_"))
        work_dir = temp_root

    try:
        # Load inputs
        existing_groups = load_json_array_optional(existing_path, "bookGroup.json")
        import_groups = load_json_array_optional(import_path, "bookGroup.json")
        existing_books = load_json_array_optional(existing_path, "bookshelf.json")
        import_books = load_json_array_optional(import_path, "bookshelf.json")
        existing_search = load_json_array_optional(existing_path, "searchHistory.json")
        import_search = load_json_array_optional(import_path, "searchHistory.json")
        existing_read = load_json_array_optional(existing_path, "readRecord.json")
        import_read = load_json_array_optional(import_path, "readRecord.json")

        # 1) bookGroup
        merged_groups, group_mapping_list = merge_book_groups(
            existing_groups,
            import_groups,
            conflict_mode=args.group_name_conflict,
        )
        group_id_map = {
            int(item["oldGroupId"]): int(item["newGroupId"]) for item in group_mapping_list
        }
        group_summary = {
            "existingCount": len(existing_groups),
            "appendedCount": len(group_mapping_list),
            "totalCount": len(merged_groups),
            "groups": group_mapping_list,
            "groupIdMap": {str(k): v for k, v in sorted(group_id_map.items())},
        }

        # 2) bookshelf
        merged_books, bookshelf_summary = merge_bookshelf(
            existing_books,
            import_books,
            group_id_map,
        )

        # 3) searchHistory
        merged_search, search_summary = merge_search_history(
            existing_search,
            import_search,
        )

        # 4) readRecord
        merged_read, read_summary = merge_read_records(
            existing_read,
            import_read,
        )

        # Optional intermediate dumps
        if args.keep_temp or args.work_dir:
            write_json(work_dir / "bookGroup.json", merged_groups)
            write_json(work_dir / "bookshelf.json", merged_books)
            write_json(work_dir / "searchHistory.json", merged_search)
            write_json(work_dir / "readRecord.json", merged_read)
            write_json(
                work_dir / "group.mapping.json",
                {
                    "groupNameConflict": args.group_name_conflict,
                    "groupIdMap": group_summary["groupIdMap"],
                    "groups": group_mapping_list,
                },
            )
            write_json(work_dir / "bookshelf.mapping.json", bookshelf_summary)
            write_json(work_dir / "searchHistory.mapping.json", search_summary)
            write_json(work_dir / "readRecord.mapping.json", read_summary)

        # 5) pack
        passthrough_count = pack_output_zip(
            output_path,
            existing_path,
            {
                "bookGroup.json": merged_groups,
                "bookshelf.json": merged_books,
                "searchHistory.json": merged_search,
                "readRecord.json": merged_read,
            },
        )

        print(f"existing: {existing_path}")
        print(f"import:   {import_path}")
        print(f"output:   {output_path}")
        print(
            f"bookGroup: +{group_summary['appendedCount']} custom "
            f"(total {group_summary['totalCount']})"
        )
        print(
            f"bookshelf: +{bookshelf_summary['appendedCount']} books, "
            f"skip {bookshelf_summary['skippedDuplicateCount']} duplicates"
        )
        print(
            f"searchHistory: +{search_summary['appendedCount']} words, "
            f"skip {search_summary['skippedDuplicateCount']}"
        )
        print(
            f"readRecord: merge {read_summary['mergedSameNameCount']} names, "
            f"+{read_summary['appendedCount']} new"
        )
        print(f"passthrough files from existing: {passthrough_count}")

        if args.verbose:
            for item in group_mapping_list:
                print(
                    f"  group + {item['oldGroupName']}: "
                    f"{item['oldGroupId']} -> {item['newGroupId']} order={item['order']}"
                )
            for item in bookshelf_summary.get("skippedDuplicates", []):
                author = item.get("author") or "(empty author)"
                print(f"  book skip {item.get('name')} / {author}")
            for item in bookshelf_summary.get("appendedBooks", []):
                author = item.get("author") or "(empty author)"
                print(
                    f"  book + {item.get('name')} / {author} "
                    f"group={item.get('group')} order={item.get('order')}"
                )

        if args.report:
            report = build_report(
                existing_path,
                import_path,
                output_path,
                args.group_name_conflict,
                group_summary,
                bookshelf_summary,
                search_summary,
                read_summary,
                passthrough_count,
            )
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            print(f"report: {report_path}")

        return 0
    finally:
        if temp_root is not None and not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
