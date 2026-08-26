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
    """Normalize source books.book values like '别名#@#真正书名'."""
    text_value_ = clean_text(value)
    if not text_value_:
        return ""
    # Source stores alternate/old title before #@#, real display name after it.
    _, sep, real_name = text_value_.partition("#@#")
    if sep:
        real_name = real_name.strip()
        if real_name:
            return real_name
    return text_value_


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


def author_from_filename(filename: str) -> str:
    """Best-effort author from a parent folder like '书名(作者)'."""
    path = filename.replace("\\", "/")
    parent = PurePosixPath(path).parent.name
    if not parent or parent in {".", ""}:
        return ""
    if "(" not in parent or not parent.endswith(")"):
        return ""
    author = parent.rsplit("(", 1)[-1]
    if author.endswith(")"):
        author = author[:-1]
    return author.strip()


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


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


def count_chapters(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def sanitize_intro(text: str) -> str:
    """正常简介中位数仅约 76 字符、P90 约 580 字符；超过 3000 字符的不可能是
    正常简介，只会是章节目录缓存、接口 JSON、图片二进制、书源列表等串味
    数据（实测最大 60 万字符），直接丢弃，不做内容形状识别。"""
    if not text:
        return ""
    if len(text) > 3000:
        return ""
    return text


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
    dates_by_file: dict[str, int] | None = None,
) -> dict[str, int]:
    """Map book filename -> durChapterTime.

    Source "sort by recent read" uses history.txt order, not per-book timestamps.
    history order is the hard rule; statistics.dates only anchors timestamps:

    - history[0]: lastReadTime if present, else dates day, else addTime/fallback
    - later history items with dates: min(dates, previous - 1)
    - later history items without dates: previous - 1
    - books absent from history: books.addTime

    This keeps legado bookshelfSort=0 order aligned with source while using
    real reading days when available.
    """
    times: dict[str, int] = {}
    add_times: dict[str, int] = {}
    for row in rows:
        filename = clean_text(row["filename"])
        if not filename:
            continue
        add_times[norm_real_name(filename)] = to_long(row["addTime"], 0)

    date_times = dates_by_file or {}

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

    fallback_first = last_read_time
    if fallback_first <= 0:
        anchors = [
            value
            for key in (norm_real_name(path) for path in ordered)
            for value in (date_times.get(key, 0), add_times.get(key, 0))
            if value > 0
        ]
        fallback_first = max(anchors, default=0)
    if fallback_first <= 0:
        fallback_first = max(add_times.values(), default=0)
    if fallback_first <= 0:
        fallback_first = int(time.time() * 1000)

    last_file_key = norm_real_name(last_file) if last_file else ""
    previous_time = 0
    for index, path in enumerate(ordered):
        key = norm_real_name(path)
        dates_ts = date_times.get(key, 0)
        add_time = add_times.get(key, 0)

        if index == 0:
            if last_file_key and key == last_file_key and last_read_time > 0:
                current = last_read_time
            elif dates_ts > 0:
                current = dates_ts
            elif add_time > 0:
                current = add_time
            else:
                current = fallback_first
        elif dates_ts > 0:
            current = min(dates_ts, previous_time - 1)
        else:
            current = previous_time - 1

        times[key] = max(current, 0)
        previous_time = times[key]

    for key, add_time in add_times.items():
        if key not in times:
            times[key] = add_time
    return times


def build_dates_by_file(stats_rows: list[sqlite3.Row]) -> dict[str, int]:
    """filename -> last reading-day timestamp from statistics.dates."""
    result: dict[str, int] = {}
    for row in stats_rows:
        filename = clean_text(row["filename"])
        if not filename:
            continue
        ts = last_day_timestamp_from_dates(text_value(row["dates"]))
        if ts <= 0:
            continue
        key = norm_real_name(filename)
        prev = result.get(key, 0)
        if ts > prev:
            result[key] = ts
    return result


def wbpub_source_dir_candidates(backup: BackupFiles, filename: str) -> list[str]:
    """List subdirectory names under the book dir holding companion data."""
    book_dir = posix_dirname(filename)
    prefix = f"{book_dir}/" if book_dir else ""
    found: set[str] = set()
    for real_name in backup.names_list:
        if not real_name.startswith(prefix):
            continue
        rel = real_name[len(prefix) :]
        parts = rel.split("/", 1)
        # Only nested paths count as a source subdir; root companions
        # like .sources and the book's own .wbpub must be excluded.
        if len(parts) == 2 and parts[0]:
            found.add(parts[0])
    return sorted(found)


def resolve_wbpub_source_key(backup: BackupFiles, filename: str) -> str:
    """Resolve the current source key of a .wbpub book for metadata lookup.

    App versions differ in what the .wbpub stores: bare source key,
    "name*key#url", JSON blobs, or unrelated text. Candidate keys are
    verified against subdirectories that actually exist; unresolved keys
    fall back to the directory carrying the most complete companion data.
    bookUrl itself is always a synthetic unique placeholder, so no URL
    extraction is attempted here.
    """
    book_dir = posix_dirname(filename)
    dirs = wbpub_source_dir_candidates(backup, filename)
    text = clean_text(read_companion_text(backup, filename))
    candidates: list[str] = []
    if text:
        candidates.append(text)
        tail = text.partition("#")[0].rpartition("*")[2].strip()
        mid = text.rpartition("*")[2].partition("#")[0].strip()
        for cand in (tail, mid):
            if cand and cand not in candidates:
                candidates.append(cand)

    for cand in candidates:
        if cand in dirs:
            return cand

    best = ""
    best_rank: tuple[int, str] = (-1, "")
    for d in dirs:
        has_chapters = backup.read_bytes(f"{book_dir}/{d}/.chapters") is not None
        rank = (1 if has_chapters else 0, d)
        if rank > best_rank:
            best_rank = rank
            best = d
    return best


def read_wbpub_meta(backup: BackupFiles, filename: str) -> CompanionMeta:
    book_dir = posix_dirname(filename)
    source_key = resolve_wbpub_source_key(backup, filename)
    sources_text = read_companion_text(backup, f"{book_dir}/.sources")
    source_name = parse_source_name(sources_text, source_key)
    source_dir = f"{book_dir}/{source_key}" if source_key else book_dir

    chapters = read_companion_text(backup, f"{source_dir}/.chapters")
    # .latestc 不再读取：新版搜书大师常把整份章节缓存写进该文件，
    # 解析出的"最新章节标题"可能有几十万字符，且这些书无可用书源、
    # 换源后 legado 会重新获取最新章节，旧值没有保留价值。
    return CompanionMeta(
        source_key=source_key,
        source_name=source_name,
        name=read_companion_text(backup, f"{source_dir}/.name"),
        author=read_companion_text(backup, f"{source_dir}/.author"),
        intro=read_companion_text(backup, f"{source_dir}/.description"),
        cover_url=read_companion_text(backup, f"{source_dir}/.cover"),
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
    # durChapterTime is special: history.txt order is hard rule; statistics.dates
    # only anchors timestamps so legado "sort by recent read" matches source.
    add_time = to_long(row["addTime"], 0)
    order = -sequence
    category = clean_text(row["category"])
    dur_chapter_index, dur_chapter_pos = positions.get(norm_real_name(filename), (0, 0))
    dur_chapter_time = dur_chapter_times.get(norm_real_name(filename), add_time)

    db_name = clean_book_display_name(row["book"])
    db_author = clean_local_text_marker(row["author"])
    db_intro = clean_text(row["description"])
    db_cover = clean_text(row["coverFile"])
    path_name = book_name_from_filename(filename)
    path_author = author_from_filename(filename)

    if is_wbpub_book(row):
        meta = read_wbpub_meta(backup, filename)
        # Field priority (user-confirmed):
        # name/author prefer mrbooks.books, then companion, then path.
        # intro prefers companion description, then books.description.
        # bookUrl/origin/originName prefer companion, else empty.
        # coverUrl prefers companion .cover, then books.coverFile.
        # bookUrl is always a unique placeholder: legado's books table uses
        # it as the primary key, and soushu source URLs are unusable in
        # legado anyway - placeholders also keep books out of the
        # update-failure group since they are never refreshed.
        book_url = f"ssds://wbpub/{sequence:05d}"
        book: dict[str, Any] = {
            "author": first_nonempty(db_author, meta.author, path_author),
            "bookUrl": book_url,
            "canUpdate": True,
            "durChapterIndex": dur_chapter_index,
            "durChapterPos": dur_chapter_pos,
            "durChapterTime": dur_chapter_time,
            "group": group_ids[favorite],
            "lastCheckCount": 0,
            "lastCheckTime": add_time,
            "latestChapterTime": add_time,
            "name": first_nonempty(db_name, meta.name, path_name),
            "order": order,
            "origin": clean_text(meta.source_key),
            "originName": clean_text(meta.source_name),
            "originOrder": 0,
            "tocUrl": "",
            "totalChapterNum": meta.total_chapter_num,
            "type": BOOK_TYPE_TEXT,
        }
        cover_url = first_nonempty(meta.cover_url, db_cover)
        if cover_url:
            book["coverUrl"] = cover_url
        intro = sanitize_intro(first_nonempty(meta.intro, db_intro))
        if intro:
            book["intro"] = intro
        if category:
            book["kind"] = category
        if meta.latest_chapter_title:
            book["latestChapterTitle"] = meta.latest_chapter_title
        dur_title = chapter_title_at(meta.chapters_text, dur_chapter_index)
        if dur_title:
            book["durChapterTitle"] = dur_title
        return book

    local_name = file_name_from_path(filename)
    local_kind = clean_local_text_marker(category)
    book = {
        "author": first_nonempty(db_author, path_author),
        "bookUrl": filename,
        "canUpdate": True,
        "durChapterIndex": dur_chapter_index,
        "durChapterPos": dur_chapter_pos,
        "durChapterTime": dur_chapter_time,
        "group": group_ids[favorite],
        "lastCheckCount": 0,
        "lastCheckTime": add_time,
        "latestChapterTime": add_time,
        "name": first_nonempty(db_name, path_name),
        "order": order,
        "origin": LOCAL_ORIGIN,
        "originName": local_name,
        "originOrder": 0,
        "tocUrl": "",
        "totalChapterNum": 0,
        "type": BOOK_TYPE_LOCAL_TEXT,
    }
    if db_cover:
        book["coverUrl"] = db_cover
    if db_intro:
        intro = sanitize_intro(db_intro)
        if intro:
            book["intro"] = intro
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
    duplicate_groups: list[dict[str, Any]],
) -> None:
    wbpub_count = sum(1 for row in rows if is_wbpub_book(row))
    local_count = len(rows) - wbpub_count
    custom_groups = [group for group in groups if group["groupId"] > 0]
    duplicate_total = sum(len(item["copies"]) for item in duplicate_groups)
    duplicate_dedup = sum(len(item["copies"]) - 1 for item in duplicate_groups)
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
        f"- 同名同作者重复：{len(duplicate_groups)} 组 / {duplicate_total} 本"
        f"（导入阅读后会被去重 {duplicate_dedup} 本）",
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
    if duplicate_groups:
        dedup_count = sum(len(item["copies"]) - 1 for item in duplicate_groups)
        total_count = sum(len(item["copies"]) for item in duplicate_groups)
        lines.extend(
            [
                "",
                "## 同名同作者重复书籍",
                "",
                f"- 共 {len(duplicate_groups)} 组 / {total_count} 本，"
                f"导入阅读后会被去重 {dedup_count} 本",
                "",
            ]
        )
        for item in duplicate_groups:
            author = item["author"] or "（无作者）"
            lines.append(f"- `{item['name']}` / {author}")
            for copy in item["copies"]:
                lines.append(f"    - [{copy['shelf']}] `{copy['path']}`")
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


def format_duplicate_lines(duplicate_groups: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in duplicate_groups:
        author = item["author"] or "（无作者）"
        lines.append(f"重复: {item['name']} / {author}")
        for copy in item["copies"]:
            lines.append(f"    [{copy['shelf']}] {copy['path']}")
    return lines


def prompt_duplicate_view(
    duplicate_groups: list[dict[str, Any]],
    total_books: int,
    dedup_count: int,
    txt_path: Path,
) -> None:
    """交互式查看重复书籍；非交互环境(EOF)自动按不查看处理。"""
    try:
        choice = input(
            f"\n重复书籍共 {len(duplicate_groups)} 组/{total_books} 本"
            f"(会被去重 {dedup_count} 本)。"
            f"输入 1 或回车结束不查看 / 2 在此列出 / 3 导出到 txt 文件: "
        ).strip()
    except EOFError:
        return
    if choice in ("", "1"):
        return
    if choice == "2":
        for line in format_duplicate_lines(duplicate_groups):
            print(line)
    elif choice == "3":
        lines = [
            f"同名同作者重复书籍 共 {len(duplicate_groups)} 组 / {total_books} 本"
            f"（导入阅读后会被去重 {dedup_count} 本）",
            "",
            *format_duplicate_lines(duplicate_groups),
        ]
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"重复书籍已导出: {txt_path}")


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
        stats_rows = read_statistics_rows(db_path)
        dates_by_file = build_dates_by_file(stats_rows)
        dur_chapter_times = build_dur_chapter_times(
            rows,
            history_files,
            last_file,
            last_read_time,
            dates_by_file,
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
        # Safety net: legado's books table uses bookUrl as primary key.
        # De-duplicate so no row can silently overwrite another on restore.
        seen_book_urls: set[str] = set()
        for book in books:
            url = book["bookUrl"]
            if url in seen_book_urls:
                suffix = 1
                while f"{url}#{suffix}" in seen_book_urls:
                    suffix += 1
                url = f"{url}#{suffix}"
                book["bookUrl"] = url
            seen_book_urls.add(url)

        # legado's books table also carries a UNIQUE(name, author) index, so
        # same-name-same-author rows collapse to one on restore. Report them
        # here so the shelf count after import matches expectations.
        group_name_by_id = {group["groupId"]: group["groupName"] for group in groups}
        by_name_author: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for book, row in zip(books, rows):
            key = (book["name"], book["author"])
            by_name_author.setdefault(key, []).append(
                {
                    "path": str(row["filename"]),
                    "shelf": group_name_by_id.get(book["group"], "?"),
                }
            )
        duplicate_groups = [
            {"name": name, "author": author, "copies": copies}
            for (name, author), copies in sorted(by_name_author.items())
            if len(copies) > 1
        ]
        duplicate_book_count = sum(len(item["copies"]) for item in duplicate_groups)
        duplicate_dedup_count = sum(len(item["copies"]) - 1 for item in duplicate_groups)
        search_history = build_search_history(read_source_search_history(backup))
        read_records = build_read_records(
            stats_rows,
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
                duplicate_groups,
            )

        wbpub_count = sum(1 for row in rows if is_wbpub_book(row))
        placeholder_count = sum(1 for b in books if str(b.get("bookUrl", "")).startswith("ssds://wbpub/"))
        custom_group_count = sum(1 for group in groups if group["groupId"] > 0)
        print(f"转换完成: {output_path}")
        print(f"书籍: {len(books)}")
        print(f"自定义书架/分组: {custom_group_count}")
        print(f"legado内置分组: {len(groups) - custom_group_count}")
        print(f"搜索历史: {len(search_history)}")
        print(f"阅读记录: {len(read_records)}")
        print(f".wbpub书源书籍: {wbpub_count}")
        print(f"真正本地书籍: {len(books) - wbpub_count}")
        print(f"占位URL网文书(需换源): {placeholder_count}")
        if duplicate_groups:
            print(
                f"同名同作者重复: {len(duplicate_groups)} 组 / {duplicate_book_count} 本, "
                f"导入阅读后会被去重 {duplicate_dedup_count} 本"
            )
        if args.verbose:
            for line in format_duplicate_lines(duplicate_groups):
                print(line)
        if report_path:
            print(f"报告: {report_path}")
        if args.keep_temp:
            print(f"临时目录: {temp_root}")
        if duplicate_groups and not args.verbose:
            prompt_duplicate_view(
                duplicate_groups,
                duplicate_book_count,
                duplicate_dedup_count,
                output_path.with_name(output_path.stem + "-重复书籍.txt"),
            )
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
