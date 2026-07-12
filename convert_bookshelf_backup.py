#!/usr/bin/env python3
"""Convert source APK bookshelf backup to a legado-E backup zip.

Data source for this converter is mrbooks.db/books.  The source APK stores
bookshelf membership in books.favorite; .wbpub companion files are used only
to enrich metadata for web-source books.
"""

from __future__ import annotations

import argparse
import re
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any


SQLITE_HEADER = b"SQLite format 3\x00"

BOOK_TYPE_TEXT = 8
BOOK_TYPE_LOCAL = 256
BOOK_TYPE_LOCAL_TEXT = BOOK_TYPE_TEXT | BOOK_TYPE_LOCAL
LOCAL_ORIGIN = "loc_book"

BUILTIN_BOOK_GROUPS: list[dict[str, Any]] = [
    {
        "bookSort": -1,
        "enableRefresh": True,
        "groupId": -1,
        "groupName": "全部",
        "order": -10,
        "show": True,
    },
    {
        "bookSort": -1,
        "enableRefresh": False,
        "groupId": -2,
        "groupName": "本地",
        "order": -9,
        "show": True,
    },
    {
        "bookSort": -1,
        "enableRefresh": True,
        "groupId": -3,
        "groupName": "音频",
        "order": -8,
        "show": True,
    },
    {
        "bookSort": -1,
        "enableRefresh": True,
        "groupId": -4,
        "groupName": "网络未分组",
        "order": -7,
        "show": True,
    },
    {
        "bookSort": -1,
        "enableRefresh": True,
        "groupId": -5,
        "groupName": "本地未分组",
        "order": -6,
        "show": False,
    },
    {
        "bookSort": -1,
        "enableRefresh": True,
        "groupId": -11,
        "groupName": "更新失败",
        "order": -1,
        "show": True,
    },
]


@dataclass
class BackupEntry:
    index: int
    real_name: str
    stored_name: str
    size: int


@dataclass
class BackupFiles:
    input_path: Path
    names_list: list[str]
    entries_by_real: dict[str, BackupEntry]
    root_dir: Path | None = None
    zip_file: zipfile.ZipFile | None = None

    def close(self) -> None:
        if self.zip_file is not None:
            self.zip_file.close()

    def read_bytes(self, real_name: str) -> bytes | None:
        entry = self.entries_by_real.get(norm_real_name(real_name))
        if entry is None:
            return None
        if self.zip_file is not None:
            with self.zip_file.open(entry.stored_name) as fp:
                return fp.read()
        if self.root_dir is None:
            return None
        path = self.root_dir / entry.stored_name
        if path.exists():
            return path.read_bytes()
        return None

    def extract_to(self, real_name: str, out_dir: Path) -> Path:
        entry = self.entries_by_real.get(norm_real_name(real_name))
        if entry is None:
            raise RuntimeError(f"未找到备份条目：{real_name}")
        suffix = Path(real_name).suffix or Path(entry.stored_name).suffix or ".bin"
        out_path = out_dir / f"entry-{entry.index}{suffix}"
        if self.zip_file is not None:
            with self.zip_file.open(entry.stored_name) as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        else:
            if self.root_dir is None:
                raise RuntimeError("内部错误：目录备份缺少 root_dir")
            shutil.copyfile(self.root_dir / entry.stored_name, out_path)
        return out_path


@dataclass
class CompanionMeta:
    source_key: str = ""
    source_name: str = ""
    detail_url: str = ""
    name: str = ""
    author: str = ""
    intro: str = ""
    cover_url: str = ""
    latest_chapter_title: str = ""
    total_chapter_num: int = 0
    chapters_text: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert source APK mrbooks.db/books backup to legado-E bookshelf backup."
    )
    parser.add_argument("backup", help="source ssds.backup/.zip path or extracted backup directory")
    parser.add_argument(
        "-o",
        "--output",
        help="output legado-E backup zip path, default: backup-converted.zip beside input",
    )
    parser.add_argument("--report", help="optional markdown report path")
    parser.add_argument("--work-dir", help="temporary work directory")
    parser.add_argument("--keep-temp", action="store_true", help="keep extracted temp files")
    parser.add_argument("--verbose", action="store_true", help="print scan details")
    return parser.parse_args()


def norm_real_name(value: str) -> str:
    return value.replace("\\", "/").strip().lower()


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def decode_names_list(data: bytes) -> list[str]:
    return [line.strip() for line in decode_text(data).splitlines()]


def is_numbered_backup_name(name: str) -> int | None:
    base = PurePosixPath(name).name
    stem = PurePosixPath(base).stem
    suffix = PurePosixPath(base).suffix.lower()
    if suffix not in {".tag", ".db"}:
        return None
    if not stem.isdigit():
        return None
    return int(stem)


def find_names_file_in_dir(path: Path) -> Path:
    direct = path / "_names.list"
    if direct.exists():
        return direct
    matches = list(path.rglob("_names.list"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("未找到 _names.list，输入目录不像源 APK 本地备份")
    raise RuntimeError("输入目录下存在多个 _names.list，请直接传入具体备份目录")


def open_backup(input_path: Path) -> BackupFiles:
    if input_path.is_dir():
        names_path = find_names_file_in_dir(input_path)
        root_dir = names_path.parent
        names_list = decode_names_list(names_path.read_bytes())
        entries: dict[str, BackupEntry] = {}
        for index, real_name in enumerate(names_list, start=1):
            stored_path = None
            for suffix in (".tag", ".db"):
                candidate = root_dir / f"{index}{suffix}"
                if candidate.exists():
                    stored_path = candidate
                    break
            if stored_path is None:
                continue
            entry = BackupEntry(
                index=index,
                real_name=real_name,
                stored_name=stored_path.name,
                size=stored_path.stat().st_size,
            )
            entries[norm_real_name(real_name)] = entry
        return BackupFiles(
            input_path=input_path,
            names_list=names_list,
            entries_by_real=entries,
            root_dir=root_dir,
        )

    zf = zipfile.ZipFile(input_path)
    names_info = next(
        (
            info
            for info in zf.infolist()
            if not info.is_dir() and PurePosixPath(info.filename).name == "_names.list"
        ),
        None,
    )
    if names_info is None:
        zf.close()
        raise RuntimeError("未找到 _names.list，输入文件不像源 APK 本地备份")
    with zf.open(names_info) as fp:
        names_list = decode_names_list(fp.read())

    entries = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        index = is_numbered_backup_name(info.filename)
        if index is None or not 1 <= index <= len(names_list):
            continue
        real_name = names_list[index - 1]
        entries[norm_real_name(real_name)] = BackupEntry(
            index=index,
            real_name=real_name,
            stored_name=info.filename,
            size=info.file_size,
        )
    return BackupFiles(
        input_path=input_path,
        names_list=names_list,
        entries_by_real=entries,
        zip_file=zf,
    )


def find_mrbooks_real_name(backup: BackupFiles) -> str:
    for real_name in backup.names_list:
        normalized = norm_real_name(real_name)
        if normalized.endswith("/databases/mrbooks.db") or normalized == "mrbooks.db":
            return real_name
    raise RuntimeError("未在 _names.list 中找到 databases/mrbooks.db")


def find_real_name_by_suffix(backup: BackupFiles, suffix: str) -> str | None:
    suffix = norm_real_name(suffix)
    for real_name in backup.names_list:
        if norm_real_name(real_name).endswith(suffix):
            return real_name
    return None


def sqlite_connect_ro(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def validate_mrbooks_db(path: Path) -> None:
    if path.read_bytes()[: len(SQLITE_HEADER)] != SQLITE_HEADER:
        raise RuntimeError("mrbooks.db 不是 SQLite 数据库")
    with sqlite_connect_ro(path) as conn:
        row = conn.execute(
            "select 1 from sqlite_master where type='table' and name='books'"
        ).fetchone()
    if row is None:
        raise RuntimeError("mrbooks.db 中没有 books 表")


def read_books_rows(db_path: Path) -> list[sqlite3.Row]:
    sql = """
        select
            _id,
            book,
            filename,
            lowerFilename,
            author,
            description,
            category,
            thumbFile,
            coverFile,
            addTime,
            favorite,
            downloadUrl,
            rate,
            bak1,
            bak2
        from books
        order by _id asc
    """
    with sqlite_connect_ro(db_path) as conn:
        return conn.execute(sql).fetchall()


def read_statistics_rows(db_path: Path) -> list[sqlite3.Row]:
    """Return statistics rows if the table exists; empty list otherwise."""
    with sqlite_connect_ro(db_path) as conn:
        row = conn.execute(
            "select 1 from sqlite_master where type='table' and name='statistics'"
        ).fetchone()
        if row is None:
            return []
        return conn.execute(
            """
            select filename, usedTime, readWords, dates
            from statistics
            order by _id asc
            """
        ).fetchall()


def clean_book_display_name(value: Any) -> str:
    """Normalize source books.book values like '书名#@#别名'."""
    text_value_ = clean_text(value)
    if not text_value_:
        return ""
    name, sep, _ = text_value_.partition("#@#")
    return name.strip() if sep else text_value_


def book_name_from_filename(filename: str) -> str:
    """Best-effort book title from a source file path."""
    path = filename.replace("\\", "/")
    stem = PurePosixPath(path).stem
    if not stem:
        return ""
    # Prefer parent folder "书名(作者)" when present.
    parent = PurePosixPath(path).parent.name
    if parent and parent not in {".", ""}:
        if "(" in parent:
            return parent.split("(", 1)[0].strip() or stem
        return parent
    return stem


def last_day_timestamp_from_dates(dates_text: str) -> int:
    """Convert statistics.dates last day-number into epoch millis at local midnight.

    dates lines look like: 20646|6542@734
    day number is days since 1970-01-01.
    """
    last_day = 0
    for line in text_value(dates_text).splitlines():
        line = line.strip()
        if not line:
            continue
        day_part, _, _ = line.partition("|")
        day = to_int(day_part, 0)
        if day > last_day:
            last_day = day
    if last_day <= 0:
        return 0
    try:
        day_date = date(1970, 1, 1) + timedelta(days=last_day)
        # Use UTC midnight; exact hour is unavailable from day-only source data.
        return int(
            datetime(
                day_date.year,
                day_date.month,
                day_date.day,
                tzinfo=timezone.utc,
            ).timestamp()
            * 1000
        )
    except (OverflowError, ValueError, OSError):
        return 0


def build_read_records(
    stats_rows: list[sqlite3.Row],
    book_rows: list[sqlite3.Row],
    last_file: str,
    last_read_time: int,
) -> list[dict[str, Any]]:
    """Map source statistics -> legado readRecord.json items.

    Precise:
      - readTime <- statistics.usedTime (accumulated reading duration)
      - bookName <- books.book (fallback: path-derived title)
      - lastRead for the single global lastFile <- options lastReadTime

    Approximate / fallback for lastRead (do NOT use history decreasing stamps):
      1) lastFile + lastReadTime, only for that one book
      2) last day in statistics.dates (day-level real reading day)
      3) books.addTime
      4) 0
    """
    meta_by_file: dict[str, tuple[str, int]] = {}
    for row in book_rows:
        filename = clean_text(row["filename"])
        if not filename:
            continue
        name = clean_book_display_name(row["book"]) or book_name_from_filename(filename)
        add_time = to_long(row["addTime"], 0)
        meta_by_file[norm_real_name(filename)] = (name, add_time)

    last_file_key = norm_real_name(last_file) if last_file else ""

    # Aggregate by bookName because legado groups statistics by book name.
    aggregated: dict[str, dict[str, int]] = {}
    for row in stats_rows:
        filename = clean_text(row["filename"])
        if not filename:
            continue
        read_time = to_long(row["usedTime"], 0)
        if read_time <= 0:
            continue

        key = norm_real_name(filename)
        if key in meta_by_file:
            book_name, add_time = meta_by_file[key]
        else:
            book_name = book_name_from_filename(filename)
            add_time = 0
        if not book_name:
            continue

        # Only the globally last-opened book has a real millisecond timestamp.
        if last_file_key and key == last_file_key and last_read_time > 0:
            last_read = last_read_time
        else:
            last_read = last_day_timestamp_from_dates(text_value(row["dates"]))
            if last_read <= 0:
                last_read = add_time

        item = aggregated.get(book_name)
        if item is None:
            aggregated[book_name] = {
                "readTime": read_time,
                "lastRead": last_read,
            }
        else:
            item["readTime"] += read_time
            if last_read > item["lastRead"]:
                item["lastRead"] = last_read

    records = [
        {
            "bookName": book_name,
            "deviceId": "",
            "lastRead": values["lastRead"],
            "readTime": values["readTime"],
        }
        for book_name, values in aggregated.items()
    ]
    records.sort(key=lambda item: (-item["readTime"], item["bookName"]))
    return records



def to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_long(value: Any, default: int = 0) -> int:
    return to_int(value, default)


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def clean_text(value: Any) -> str:
    return text_value(value).strip()


def posix_dirname(path: str) -> str:
    return str(PurePosixPath(path).parent)


def read_companion_text(backup: BackupFiles, real_name: str) -> str:
    data = backup.read_bytes(real_name)
    if data is None:
        return ""
    try:
        data = zlib.decompress(data)
    except zlib.error:
        pass
    return decode_text(data).strip()


def read_plain_backup_text(backup: BackupFiles, real_name: str) -> str:
    data = backup.read_bytes(real_name)
    if data is None:
        return ""
    return decode_text(data).strip()


def read_source_search_history(backup: BackupFiles) -> list[str]:
    real_name = find_real_name_by_suffix(backup, "/shared_prefs/web_book_search")
    if real_name is None:
        return []
    text = read_plain_backup_text(backup, real_name)
    words: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        word = line.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words


def read_source_shelf_names(backup: BackupFiles) -> list[str]:
    real_name = find_real_name_by_suffix(backup, "/shared_prefs/shelf_names.txt")
    if real_name is None:
        return []
    text = read_plain_backup_text(backup, real_name)
    names: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        name = line.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def build_search_history(words: list[str]) -> list[dict[str, Any]]:
    base_time = int(time.time() * 1000)
    return [
        {
            "lastUseTime": base_time - index,
            "usage": 1,
            "word": word,
        }
        for index, word in enumerate(words)
    ]


def parse_source_name(text: str, source_key: str) -> str:
    if not source_key:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        source_part, _, _ = line.partition("#")
        source_name, _, item_key = source_part.partition("*")
        if item_key.strip() == source_key:
            return source_name.strip()
    return ""


def parse_latest_title(text: str) -> str:
    if not text:
        return ""
    _, sep, title = text.partition("*")
    return title.strip() if sep else text.strip()


def count_chapters(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def chapter_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def chapter_title_at(text: str, index: int) -> str:
    lines = chapter_lines(text)
    if index < 0 or index >= len(lines):
        return ""
    title, _, _ = lines[index].partition("#*#")
    return title.strip()


def parse_position_value(value: str) -> tuple[int, int]:
    """Parse positions10.xml value like '12@0#0:0.0%'.

    chapter index is 0-based: reading chapter 1 => 0.
    """
    text = clean_text(value)
    if not text:
        return 0, 0
    head, _, _ = text.partition(":")
    chapter_part, _, rest = head.partition("@")
    _, _, pos_part = rest.partition("#")
    return to_int(chapter_part, 0), to_int(pos_part, 0)


def read_source_positions(backup: BackupFiles) -> dict[str, tuple[int, int]]:
    real_name = find_real_name_by_suffix(backup, "/shared_prefs/positions10.xml")
    if real_name is None:
        return {}
    text = read_plain_backup_text(backup, real_name)
    positions: dict[str, tuple[int, int]] = {}
    for match in re.finditer(
        r'<string\s+name="([^"]+)">([^<]*)</string>',
        text,
        flags=re.I,
    ):
        key = norm_real_name(match.group(1))
        positions[key] = parse_position_value(match.group(2))
    return positions


def read_source_last_read(backup: BackupFiles) -> tuple[str, int]:
    """Return (lastFile, lastReadTime) from options1002.xml if present."""
    real_name = find_real_name_by_suffix(backup, "/shared_prefs/options1002.xml")
    if real_name is None:
        return "", 0
    text = read_plain_backup_text(backup, real_name)
    last_file = ""
    last_read_time = 0
    m_file = re.search(r'<string\s+name="lastFile">([^<]*)</string>', text, flags=re.I)
    if m_file:
        last_file = clean_text(m_file.group(1))
    m_time = re.search(r'<long\s+name="lastReadTime"\s+value="([^"]+)"\s*/>', text, flags=re.I)
    if m_time:
        last_read_time = to_long(m_time.group(1), 0)
    return last_file, last_read_time


def read_source_history_files(backup: BackupFiles) -> list[str]:
    """Return recent-open file paths from history.txt, most recent first."""
    real_name = find_real_name_by_suffix(backup, "/shared_prefs/history.txt")
    if real_name is None:
        return []
    text = read_plain_backup_text(backup, real_name)
    files: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        path = clean_text(line)
        if not path:
            continue
        key = norm_real_name(path)
        if key in seen:
            continue
        seen.add(key)
        files.append(path)
    return files


def build_dur_chapter_times(
    rows: list[sqlite3.Row],
    history_files: list[str],
    last_file: str,
    last_read_time: int,
) -> dict[str, int]:
    """Map book filename -> durChapterTime.

    Source "sort by recent read" uses history.txt order, not per-book timestamps.
    To make legado bookshelfSort=0 match that order:

    - history[0] = lastReadTime (or fallback base)
    - history[1] = base - 1
    - history[2] = base - 2
    - books absent from history keep books.addTime
    """
    times: dict[str, int] = {}
    add_times: dict[str, int] = {}
    for row in rows:
        filename = clean_text(row["filename"])
        if not filename:
            continue
        add_times[norm_real_name(filename)] = to_long(row["addTime"], 0)

    base = last_read_time
    if base <= 0:
        base = max(add_times.values(), default=0)
    if base <= 0:
        base = int(time.time() * 1000)

    # Ensure lastFile is treated as most recent when history is empty/missing it.
    ordered: list[str] = []
    seen: set[str] = set()
    if last_file:
        key = norm_real_name(last_file)
        if key in add_times and key not in seen:
            ordered.append(last_file)
            seen.add(key)
    for path in history_files:
        key = norm_real_name(path)
        if key not in add_times or key in seen:
            continue
        ordered.append(path)
        seen.add(key)

    for index, path in enumerate(ordered):
        times[norm_real_name(path)] = base - index

    for key, add_time in add_times.items():
        if key not in times:
            times[key] = add_time
    return times


def read_wbpub_meta(backup: BackupFiles, filename: str) -> CompanionMeta:
    book_dir = posix_dirname(filename)
    source_key = read_companion_text(backup, filename)
    sources_text = read_companion_text(backup, f"{book_dir}/.sources")
    source_name = parse_source_name(sources_text, source_key)
    source_dir = f"{book_dir}/{source_key}" if source_key else book_dir

    chapters = read_companion_text(backup, f"{source_dir}/.chapters")
    return CompanionMeta(
        source_key=source_key,
        source_name=source_name,
        detail_url=read_companion_text(backup, f"{source_dir}/.url"),
        name=read_companion_text(backup, f"{source_dir}/.name"),
        author=read_companion_text(backup, f"{source_dir}/.author"),
        intro=read_companion_text(backup, f"{source_dir}/.description"),
        cover_url=read_companion_text(backup, f"{source_dir}/.cover"),
        latest_chapter_title=parse_latest_title(
            read_companion_text(backup, f"{source_dir}/.latestc")
        ),
        total_chapter_num=count_chapters(chapters),
        chapters_text=chapters,
    )


def is_wbpub_book(row: sqlite3.Row) -> bool:
    return clean_text(row["filename"]).lower().endswith(".wbpub")


def file_name_from_path(path: str) -> str:
    if not path:
        return ""
    return PurePosixPath(path.replace("\\", "/")).name


def clean_local_text_marker(value: Any) -> str:
    text = clean_text(value)
    if text.upper() == "(TXT)":
        return ""
    return text


def build_groups(
    rows: list[sqlite3.Row],
    shelf_names: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    groups: list[dict[str, Any]] = [dict(group) for group in BUILTIN_BOOK_GROUPS]
    group_ids: dict[str, int] = {}

    ordered_group_names: list[str] = []
    seen_names: set[str] = set()
    for name in shelf_names:
        if name in seen_names:
            continue
        seen_names.add(name)
        ordered_group_names.append(name)
    for row in rows:
        group_name = clean_text(row["favorite"])
        if group_name in seen_names:
            continue
        seen_names.add(group_name)
        ordered_group_names.append(group_name)

    for group_name in ordered_group_names:
        if group_name in group_ids:
            continue
        if len(group_ids) >= 63:
            raise RuntimeError("legado-E 自定义书架分组最多支持 63 个正数位标记")
        group_id = 1 << len(group_ids)
        group_ids[group_name] = group_id
        groups.append(
            {
                "bookSort": -1,
                "enableRefresh": True,
                "groupId": group_id,
                "groupName": group_name,
                "order": len(group_ids),
                "show": True,
            }
        )
    return groups, group_ids


def row_to_book(
    row: sqlite3.Row,
    backup: BackupFiles,
    group_ids: dict[str, int],
    sequence: int,
    positions: dict[str, tuple[int, int]],
    dur_chapter_times: dict[str, int],
) -> dict[str, Any]:
    """Build one legado Book JSON object.

    Critical status fields are always written with explicit values.
    Optional nullable fields are only included when source data exists.
    """
    filename = clean_text(row["filename"])
    favorite = clean_text(row["favorite"])
    # Source books.addTime is shelf-add time. Legado has no dedicated addTime field;
    # newly created books fill latestChapterTime/lastCheckTime/durChapterTime at creation,
    # so map addTime into those three as "added at this time".
    # durChapterTime is special: history.txt order is expanded into decreasing timestamps
    # so legado "sort by recent read" matches source.
    add_time = to_long(row["addTime"], 0)
    order = -sequence
    category = clean_text(row["category"])
    dur_chapter_index, dur_chapter_pos = positions.get(norm_real_name(filename), (0, 0))
    dur_chapter_time = dur_chapter_times.get(norm_real_name(filename), add_time)

    if is_wbpub_book(row):
        meta = read_wbpub_meta(backup, filename)
        book: dict[str, Any] = {
            "author": meta.author,
            "bookUrl": meta.detail_url,
            "canUpdate": True,
            "durChapterIndex": dur_chapter_index,
            "durChapterPos": dur_chapter_pos,
            "durChapterTime": dur_chapter_time,
            "group": group_ids[favorite],
            "lastCheckCount": 0,
            "lastCheckTime": add_time,
            "latestChapterTime": add_time,
            "name": meta.name,
            "order": order,
            "origin": meta.source_key,
            "originName": meta.source_name,
            "originOrder": 0,
            "tocUrl": "",
            "totalChapterNum": meta.total_chapter_num,
            "type": BOOK_TYPE_TEXT,
        }
        if meta.cover_url:
            book["coverUrl"] = meta.cover_url
        if meta.intro:
            book["intro"] = meta.intro
        if category:
            book["kind"] = category
        if meta.latest_chapter_title:
            book["latestChapterTitle"] = meta.latest_chapter_title
        dur_title = chapter_title_at(meta.chapters_text, dur_chapter_index)
        if dur_title:
            book["durChapterTitle"] = dur_title
        return book

    local_name = file_name_from_path(filename)
    local_author = clean_local_text_marker(row["author"])
    local_kind = clean_local_text_marker(category)
    local_cover = clean_text(row["coverFile"])
    local_intro = text_value(row["description"])
    book = {
        "author": local_author,
        "bookUrl": filename,
        "canUpdate": True,
        "durChapterIndex": dur_chapter_index,
        "durChapterPos": dur_chapter_pos,
        "durChapterTime": dur_chapter_time,
        "group": group_ids[favorite],
        "lastCheckCount": 0,
        "lastCheckTime": add_time,
        "latestChapterTime": add_time,
        "name": clean_text(row["book"]),
        "order": order,
        "origin": LOCAL_ORIGIN,
        "originName": local_name,
        "originOrder": 0,
        "tocUrl": "",
        "totalChapterNum": 0,
        "type": BOOK_TYPE_LOCAL_TEXT,
    }
    if local_cover:
        book["coverUrl"] = local_cover
    if local_intro:
        book["intro"] = local_intro
    if local_kind:
        book["kind"] = local_kind
    return book


def omit_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [omit_none(item) for item in value]
    return value


def write_output_zip(
    output_path: Path,
    books: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    search_history: list[dict[str, Any]],
    read_records: list[dict[str, Any]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "bookshelf.json",
            json.dumps(omit_none(books), ensure_ascii=False, indent=2, sort_keys=True),
        )
        zf.writestr(
            "bookGroup.json",
            json.dumps(omit_none(groups), ensure_ascii=False, indent=2, sort_keys=True),
        )
        if search_history:
            zf.writestr(
                "searchHistory.json",
                json.dumps(search_history, ensure_ascii=False, indent=2, sort_keys=True),
            )
        if read_records:
            zf.writestr(
                "readRecord.json",
                json.dumps(read_records, ensure_ascii=False, indent=2, sort_keys=True),
            )


def write_report(
    report_path: Path,
    backup_path: Path,
    output_path: Path,
    mrbooks_real_name: str,
    rows: list[sqlite3.Row],
    books: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    search_history: list[dict[str, Any]],
    read_records: list[dict[str, Any]],
) -> None:
    wbpub_count = sum(1 for row in rows if is_wbpub_book(row))
    local_count = len(rows) - wbpub_count
    custom_groups = [group for group in groups if group["groupId"] > 0]
    lines = [
        "# 转换报告",
        "",
        f"- 输入备份：`{backup_path}`",
        f"- 输出备份：`{output_path}`",
        f"- 使用数据库：`{mrbooks_real_name}`",
        f"- 书籍数量：{len(books)}",
        f"- 自定义书架/分组数量：{len(custom_groups)}",
        f"- legado 内置分组数量：{len(groups) - len(custom_groups)}",
        f"- 搜索历史数量：{len(search_history)}",
        f"- 阅读记录数量：{len(read_records)}",
        f"- .wbpub 书源书籍：{wbpub_count}",
        f"- 真正本地书籍：{local_count}",
        "",
        "## 源书架对应的自定义分组",
        "",
    ]
    for group in custom_groups:
        lines.append(
            f"- `{group['groupName']}`：groupId={group['groupId']}，order={group['order']}"
        )
    lines.extend(["", "## 书籍", ""])
    for book in books:
        lines.append(
            f"- `{book['name']}`：group={book['group']}，origin=`{book['origin']}`，bookUrl=`{book['bookUrl']}`"
        )
    if search_history:
        lines.extend(["", "## 搜索历史", ""])
        for item in search_history:
            lines.append(f"- `{item['word']}`")
    if read_records:
        lines.extend(["", "## 阅读记录", ""])
        for item in read_records[:50]:
            lines.append(
                f"- `{item['bookName']}`：readTime={item['readTime']}，lastRead={item['lastRead']}"
            )
        if len(read_records) > 50:
            lines.append(f"- ... 其余 {len(read_records) - 50} 条省略")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> int:
    args = parse_args()
    backup_path = Path(args.backup).resolve()
    if not backup_path.exists():
        raise RuntimeError(f"输入备份不存在：{backup_path}")

    output_path = (
        Path(args.output).resolve()
        if args.output
        else backup_path.with_name("backup-converted.zip")
    )
    report_path = Path(args.report).resolve() if args.report else None
    if backup_path == output_path:
        raise RuntimeError("输出文件不能和输入备份相同")

    cleanup_temp = not args.keep_temp
    if args.work_dir:
        temp_root = Path(args.work_dir).resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="bookshelf-convert-"))

    backup: BackupFiles | None = None
    try:
        backup = open_backup(backup_path)
        mrbooks_real_name = find_mrbooks_real_name(backup)
        db_path = backup.extract_to(mrbooks_real_name, temp_root)
        validate_mrbooks_db(db_path)
        rows = read_books_rows(db_path)
        shelf_names = read_source_shelf_names(backup)
        groups, group_ids = build_groups(rows, shelf_names)
        positions = read_source_positions(backup)
        last_file, last_read_time = read_source_last_read(backup)
        history_files = read_source_history_files(backup)
        dur_chapter_times = build_dur_chapter_times(
            rows,
            history_files,
            last_file,
            last_read_time,
        )
        books = [
            row_to_book(
                row,
                backup,
                group_ids,
                sequence,
                positions,
                dur_chapter_times,
            )
            for sequence, row in enumerate(rows, start=1)
        ]
        search_history = build_search_history(read_source_search_history(backup))
        read_records = build_read_records(
            read_statistics_rows(db_path),
            rows,
            last_file,
            last_read_time,
        )
        write_output_zip(output_path, books, groups, search_history, read_records)
        if report_path:
            write_report(
                report_path,
                backup_path,
                output_path,
                mrbooks_real_name,
                rows,
                books,
                groups,
                search_history,
                read_records,
            )

        wbpub_count = sum(1 for row in rows if is_wbpub_book(row))
        custom_group_count = sum(1 for group in groups if group["groupId"] > 0)
        print(f"转换完成: {output_path}")
        print(f"书籍: {len(books)}")
        print(f"自定义书架/分组: {custom_group_count}")
        print(f"legado内置分组: {len(groups) - custom_group_count}")
        print(f"搜索历史: {len(search_history)}")
        print(f"阅读记录: {len(read_records)}")
        print(f".wbpub书源书籍: {wbpub_count}")
        print(f"真正本地书籍: {len(books) - wbpub_count}")
        if report_path:
            print(f"报告: {report_path}")
        if args.keep_temp:
            print(f"临时目录: {temp_root}")
        return 0
    finally:
        if backup is not None:
            backup.close()
        if cleanup_temp and temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)


def main() -> None:
    try:
        raise SystemExit(run())
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
