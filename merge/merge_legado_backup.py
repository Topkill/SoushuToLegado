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


def reading_stats(rows: list[Any]) -> tuple[int, int]:
    """返回 (有效记录数, 总阅读毫秒数)，忽略空书名的无效记录。"""
    count = 0
    total_ms = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if not str(raw.get("bookName") or "").strip():
            continue
        count += 1
        try:
            total_ms += max(0, int(raw.get("readTime") or 0))
        except (TypeError, ValueError):
            continue
    return count, total_ms


def format_duration(ms: int) -> str:
    seconds = ms // 1000
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if days or hours:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分{secs}秒")
    return "".join(parts)


def build_report(
    existing_path: Path,
    import_path: Path,
    output_path: Path,
    group_mode: str,
    group_summary: dict[str, Any],
    bookshelf_summary: dict[str, Any],
    search_summary: dict[str, Any],
    read_summary: dict[str, Any],
    before_count: int,
    before_ms: int,
    after_count: int,
    after_ms: int,
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
            f"- 去除同名同作者：{bookshelf_summary.get('skippedDuplicateCount', 0)} 本"
            f"（与现有重名 {bookshelf_summary.get('skippedExistingMatchCount', 0)} 本，"
            f"组内副本 {bookshelf_summary.get('skippedImportDuplicateCount', 0)} 本）",
            f"- 源数据含重复：{bookshelf_summary.get('importDuplicateGroups', 0)} 组"
            f"/{bookshelf_summary.get('importDuplicateBooks', 0)} 本",
            f"- 追加：{bookshelf_summary.get('appendedCount', 0)}",
            f"- 合并后一共：{bookshelf_summary.get('mergedCount', 0)} 本",
            f"- 合并结果同名同作者：{bookshelf_summary.get('remainingDuplicateGroups', 0)} 组"
            f"（导入阅读后会被去重 {bookshelf_summary.get('remainingDuplicateCount', 0)} 本）",
            "",
        ]
    )
    for item in bookshelf_summary.get("skippedDuplicates", []):
        author = item.get("author") or "（无作者）"
        lines.append(f"- 跳过 `{item.get('name')}` / {author}")
    for item in bookshelf_summary.get("appendedBooks", []):
        author = item.get("author") or "（无作者）"
        lines.append(
            f"- 新增 `{item.get('name')}` / {author} "
            f"分组={item.get('group')} 顺序={item.get('order')}"
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
            f"- 合并前阅读记录：{before_count} 本",
            f"- 合并后阅读记录：{after_count} 本",
            f"- 合并前阅读时长：{format_duration(before_ms)}（{before_ms:,} 毫秒）",
            f"- 合并后阅读时长：{format_duration(after_ms)}（{after_ms:,} 毫秒）",
            "",
            "## 注意",
            "",
            "- 本地书只迁移书架记录，不迁移文件本身。",
            "- 搜书网文书通常没有可用 Legado 书源，可能需要换源。",
            "- 同名同作者导入书会跳过，以现有 Legado 为准。",
            "- legado 对书名+作者有唯一索引，合并结果里若仍有同名同作者的书，导入阅读后也会被去重。",
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

        before_count, before_ms = reading_stats(existing_read)
        after_count, after_ms = reading_stats(merged_read)
        dropped_existing = len(existing_read) - before_count

        # legado 对 (name, author) 有唯一索引，合并结果里若仍存在同名同作者
        # 的书，导入阅读后会被 App 再去重一次。这里统计给用户预期。
        remain_na: dict[tuple[str, str], int] = {}
        for book in merged_books:
            key = (book["name"], book["author"])
            remain_na[key] = remain_na.get(key, 0) + 1
        remain_groups = sum(1 for count in remain_na.values() if count > 1)
        remain_total = sum(count for count in remain_na.values() if count > 1)
        remain_dedup = sum(count - 1 for count in remain_na.values() if count > 1)
        bookshelf_summary["remainingDuplicateGroups"] = remain_groups
        bookshelf_summary["remainingDuplicateCount"] = remain_dedup
        bookshelf_summary["mergedCount"] = len(merged_books)

        # 导入备份内部的同名同作者重复（转换脚本不去重，原样保留）。
        import_na: dict[tuple[str, str], int] = {}
        for book in import_books:
            key = (
                str(book.get("name") or "").strip(),
                str(book.get("author") or "").strip(),
            )
            import_na[key] = import_na.get(key, 0) + 1
        import_dup_groups = sum(1 for count in import_na.values() if count > 1)
        import_dup_total = sum(count for count in import_na.values() if count > 1)
        bookshelf_summary["importDuplicateGroups"] = import_dup_groups
        bookshelf_summary["importDuplicateBooks"] = import_dup_total

        print(f"现有备份: {existing_path}")
        print(f"导入备份: {import_path}")
        print(f"输出文件: {output_path}")
        print(
            f"分组: 现有 {len(existing_groups)} 个, "
            f"新增 {group_summary['appendedCount']} 个, "
            f"共 {len(merged_groups)} 个"
        )
        print(
            f"书架: 现有 {len(existing_books)} 本 "
            f"+ 实际新增 {bookshelf_summary['appendedCount']} 本"
            f"（导入 {bookshelf_summary['importCount']} 本，"
            f"去除重复的同名同作者 {bookshelf_summary['skippedDuplicateCount']} 本 "
            f"= 导入备份与现有备份重复 {bookshelf_summary['skippedExistingMatchCount']} 本"
            f" + 导入备份内重复 {bookshelf_summary['skippedImportDuplicateCount']} 本；"
            f"含重复 {import_dup_groups} 组/{import_dup_total} 本）"
            f" = 合并后一共 {len(merged_books)} 本"
        )
        if remain_groups:
            print(
                f"提示: 合并结果仍含同名同作者 {remain_groups} 组 / {remain_total} 本, "
                f"导入阅读后会被去重 {remain_dedup} 本"
            )
        print(
            f"搜索历史: 现有 {len(existing_search)} 条 "
            f"+ 实际新增 {search_summary['appendedCount']} 条"
            f"（导入 {len(import_search)} 条，"
            f"去除重复 {search_summary['skippedDuplicateCount']} 条）"
            f" = 一共 {len(merged_search)} 条"
        )
        print(
            f"阅读记录: 合并前 {before_count} 本 "
            f"+ 实际新增 {read_summary['appendedCount']} 本"
            f"（导入 {read_summary['importRowsScanned']} 本，"
            f"其中同名 {read_summary['mergedSameNameCount']} 本并入现有记录）"
            f" = 合并后 {after_count} 本"
        )
        print(
            f"阅读时长: 合并前 {format_duration(before_ms)} ({before_ms:,} 毫秒), "
            f"合并后 {format_duration(after_ms)} ({after_ms:,} 毫秒)"
        )
        if read_summary.get("skippedEmptyCount"):
            print(f"提示: 导入备份中跳过空书名阅读记录 {read_summary['skippedEmptyCount']} 条")
        if dropped_existing:
            print(f"提示: 现有备份中跳过空书名阅读记录 {dropped_existing} 条")
        print(f"透传现有备份其他文件: {passthrough_count} 个")

        if args.verbose:
            for item in group_mapping_list:
                print(
                    f"  分组新增 {item['oldGroupName']}: "
                    f"{item['oldGroupId']} -> {item['newGroupId']} 顺序={item['order']}"
                )
            for item in bookshelf_summary.get("skippedDuplicates", []):
                author = item.get("author") or "（无作者）"
                print(f"  书籍跳过 {item.get('name')} / {author}")
            for item in bookshelf_summary.get("appendedBooks", []):
                author = item.get("author") or "（无作者）"
                print(
                    f"  书籍新增 {item.get('name')} / {author} "
                    f"分组={item.get('group')} 顺序={item.get('order')}"
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
                before_count,
                before_ms,
                after_count,
                after_ms,
                passthrough_count,
            )
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            print(f"报告: {report_path}")

        return 0
    finally:
        if temp_root is not None and not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
