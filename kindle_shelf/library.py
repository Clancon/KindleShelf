from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from .articles import Article, ArticleExtractor
from .converter import CalibreConverter


class LibraryError(RuntimeError):
    pass


SUPPORTED_UPLOADS = {
    ".epub",
    ".pdf",
    ".mobi",
    ".azw",
    ".azw3",
    ".txt",
    ".html",
    ".htm",
    ".docx",
    ".rtf",
    ".cbz",
}
NATIVE_FORMATS = {".pdf", ".mobi", ".azw", ".azw3", ".txt"}
READING_STATUSES = {"unread", "reading", "finished", "abandoned"}
SCHEMA_VERSION = "1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_title(value: str, fallback: str = "未命名") -> str:
    clean = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", value or "")
    clean = re.sub(r"\s+", " ", clean).strip().strip(".")
    return (clean or fallback)[:240]


def _safe_path_component(value: str) -> bool:
    """Keep stored paths portable across Windows and Linux."""
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not re.search(r'[\x00-\x1f<>:"/\\|?*]', value)
        and not value.endswith((" ", "."))
        and not re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I)
    )


class ShelfLibrary:
    """File library plus a persistent SQLite reading-history database."""

    def __init__(
        self,
        data_dir: Path,
        converter: CalibreConverter,
        article_extractor: ArticleExtractor,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.books_dir = self.data_dir / "books"
        self.temp_dir = self.data_dir / ".working"
        self.trash_dir = self.data_dir / ".trash"
        self.database_path = self.data_dir / "kindle_shelf.db"
        self.legacy_index_path = self.data_dir / "library.json"
        self.converter = converter
        self.article_extractor = article_extractor
        self._file_lock = threading.RLock()
        for directory in (self.books_dir, self.temp_dir, self.trash_dir):
            self._validate_storage_directory(directory)
            directory.mkdir(parents=True, exist_ok=True)
        for filename in (self.database_path, self.legacy_index_path):
            self._validate_index_path(filename)
        self._initialize_database()
        self._migrate_legacy_json()

    def _validate_storage_directory(self, directory: Path) -> Path:
        if directory.resolve() != directory or directory.is_symlink():
            raise LibraryError("书库目录必须是数据目录内的真实目录。")
        return directory

    def _validate_index_path(self, filename: Path) -> None:
        if filename.resolve() != filename or filename.is_symlink():
            raise LibraryError("书库索引必须是数据目录内的真实文件。")

    def _book_directory(self, book_id: str) -> Path:
        if not _safe_path_component(book_id):
            raise LibraryError("书籍标识包含不安全的文件路径。")
        self._validate_storage_directory(self.books_dir)
        directory = self.books_dir / book_id
        if directory.resolve() != directory or directory.is_symlink():
            raise LibraryError("书籍文件目录不能通过链接指向书库之外。")
        return directory

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._validate_index_path(self.database_path)
        connection = sqlite3.connect(self.database_path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            if write:
                # Serialize before reading current state so event history records
                # the actual preceding write even with concurrent web requests.
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone():
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if version and version["value"] != SCHEMA_VERSION:
                    raise LibraryError("数据库版本不兼容，请使用创建此数据库的程序版本。")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    added_at TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    format TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size >= 0),
                    status TEXT NOT NULL DEFAULT 'unread'
                        CHECK(status IN ('unread', 'reading', 'finished', 'abandoned')),
                    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
                    started_at TEXT,
                    completed_at TEXT,
                    last_read_at TEXT,
                    updated_at TEXT NOT NULL,
                    download_count INTEGER NOT NULL DEFAULT 0 CHECK(download_count >= 0),
                    last_downloaded_at TEXT,
                    removed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS reading_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    old_status TEXT,
                    new_status TEXT,
                    old_progress INTEGER,
                    new_progress INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_books_status ON books(status, removed_at);
                CREATE INDEX IF NOT EXISTS idx_books_added ON books(added_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_book_time
                    ON reading_events(book_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_type_time
                    ON reading_events(event_type, occurred_at DESC);
                INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1');
                """
            )

    def _migrate_legacy_json(self) -> None:
        with self._connect(write=True) as connection:
            migrated = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'legacy_json_migrated'"
            ).fetchone()
            if migrated:
                return
            if not self.legacy_index_path.exists():
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('legacy_json_migrated', ?)",
                    (utc_now(),),
                )
                return
            self._validate_index_path(self.legacy_index_path)
            try:
                legacy_books = json.loads(self.legacy_index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LibraryError(f"旧书库索引无法迁移：{exc}") from exc
            if not isinstance(legacy_books, list):
                raise LibraryError("旧书库索引格式不正确，无法迁移。")

            now = utc_now()
            for position, item in enumerate(legacy_books, 1):
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise LibraryError(f"旧书库第 {position} 条记录格式不正确，无法迁移。")
                book_id = item["id"]
                filename = item.get("filename")
                if not _safe_path_component(filename):
                    raise LibraryError(f"旧书库第 {position} 条记录的文件名不安全。")
                self._book_directory(book_id)
                size = item.get("size", 0)
                if isinstance(size, bool) or not isinstance(size, (int, str)):
                    raise LibraryError(f"旧书库第 {position} 条记录的文件大小不正确。")
                try:
                    size = int(size)
                except ValueError as exc:
                    raise LibraryError(f"旧书库第 {position} 条记录的文件大小不正确。") from exc
                if not 0 <= size <= 2**63 - 1:
                    raise LibraryError(f"旧书库第 {position} 条记录的文件大小超出范围。")
                added_at = str(item.get("added_at") or now)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO books(
                        id, title, author, source_type, source_url, added_at,
                        filename, format, size, status, progress, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', 0, ?)
                    """,
                    (
                        book_id,
                        safe_title(str(item.get("title") or "")),
                        str(item.get("author") or ""),
                        str(item.get("source_type") or "upload"),
                        str(item.get("source_url") or ""),
                        added_at,
                        filename,
                        str(item.get("format") or "").upper(),
                        size,
                        added_at,
                    ),
                )
                if cursor.rowcount:
                    self._add_event(
                        connection,
                        book_id,
                        "migrated",
                        occurred_at=now,
                        note="从 library.json 自动迁移",
                    )
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('legacy_json_migrated', ?)",
                (now,),
            )

    @staticmethod
    def _add_event(
        connection: sqlite3.Connection,
        book_id: str,
        event_type: str,
        *,
        old_status: str | None = None,
        new_status: str | None = None,
        old_progress: int | None = None,
        new_progress: int | None = None,
        note: str = "",
        occurred_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO reading_events(
                book_id, event_type, old_status, new_status,
                old_progress, new_progress, note, occurred_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                book_id,
                event_type,
                old_status,
                new_status,
                old_progress,
                new_progress,
                note[:500],
                occurred_at or utc_now(),
            ),
        )

    @staticmethod
    def _book_dict(row: sqlite3.Row) -> dict:
        return dict(row)

    def list(
        self,
        query: str = "",
        status: str = "",
        *,
        include_removed: bool = False,
    ) -> list[dict]:
        clauses = [] if include_removed else ["removed_at IS NULL"]
        parameters: list[object] = []
        if query:
            clauses.append("(title LIKE ? OR author LIKE ? OR source_url LIKE ?)")
            pattern = f"%{query}%"
            parameters.extend([pattern, pattern, pattern])
        if status:
            if status not in READING_STATUSES:
                raise LibraryError("未知的阅读状态。")
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM books{where} ORDER BY added_at DESC", parameters
            ).fetchall()
        return [self._book_dict(row) for row in rows]

    def get(self, book_id: str, *, include_removed: bool = False) -> dict | None:
        removed_clause = "" if include_removed else " AND removed_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM books WHERE id = ?{removed_clause}", (book_id,)
            ).fetchone()
        return self._book_dict(row) if row else None

    def add_upload(
        self,
        stream: BinaryIO,
        filename: str,
        *,
        title: str = "",
        author: str = "",
        target: str = "auto",
    ) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_UPLOADS:
            raise LibraryError(
                "不支持此文件格式。可上传 EPUB、PDF、MOBI、AZW3、TXT、HTML、DOCX、RTF 或 CBZ。"
            )
        book_id, work_dir = self._new_work_dir()
        try:
            source = work_dir / f"source{suffix}"
            with source.open("wb") as destination:
                shutil.copyfileobj(stream, destination)
            if source.stat().st_size == 0:
                raise LibraryError("上传的文件是空的。")
            clean_title = safe_title(title, Path(filename).stem)
            clean_author = safe_title(author, "") if author else ""
            output = self._prepare_output(source, work_dir, target, clean_title, clean_author)
            return self._commit(
                book_id,
                work_dir,
                output,
                title=clean_title,
                author=clean_author,
                source_type="upload",
                source_url="",
            )
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    def add_article(self, article: Article, target: str = "mobi") -> dict:
        book_id, work_dir = self._new_work_dir()
        try:
            source = self.article_extractor.build_book_html(article, work_dir)
            output = self._prepare_output(source, work_dir, target, article.title, article.author)
            return self._commit(
                book_id,
                work_dir,
                output,
                title=safe_title(article.title),
                author=safe_title(article.author, "") if article.author else "",
                source_type="url",
                source_url=article.url,
            )
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    def add_text(self, title: str, author: str, text: str, target: str = "mobi") -> dict:
        if not text.strip():
            raise LibraryError("正文不能为空。")
        clean_title = safe_title(title)
        clean_author = safe_title(author, "") if author else ""
        book_id, work_dir = self._new_work_dir()
        try:
            source = self.article_extractor.build_text_html(
                clean_title, clean_author, text, work_dir
            )
            output = self._prepare_output(source, work_dir, target, clean_title, clean_author)
            return self._commit(
                book_id,
                work_dir,
                output,
                title=clean_title,
                author=clean_author,
                source_type="text",
                source_url="",
            )
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    def _new_work_dir(self) -> tuple[str, Path]:
        self._validate_storage_directory(self.temp_dir)
        book_id = uuid.uuid4().hex
        work_dir = self.temp_dir / book_id
        work_dir.mkdir(parents=True, exist_ok=False)
        return book_id, work_dir

    def _prepare_output(
        self, source: Path, work_dir: Path, target: str, title: str, author: str
    ) -> Path:
        target = target.lower()
        if target not in {"auto", "mobi", "azw3"}:
            raise LibraryError("未知的输出格式。")
        source_suffix = source.suffix.lower()
        if target == "auto" and source_suffix in NATIVE_FORMATS:
            output = work_dir / f"book{source_suffix}"
            shutil.copy2(source, output)
            return output

        output_suffix = ".mobi" if target == "auto" else f".{target}"
        output = work_dir / f"book{output_suffix}"
        self.converter.convert(source, output, title=title, author=author)
        return output

    def _commit(
        self,
        book_id: str,
        work_dir: Path,
        output: Path,
        *,
        title: str,
        author: str,
        source_type: str,
        source_url: str,
    ) -> dict:
        final_dir = self._book_directory(book_id)
        final_dir.mkdir(parents=True, exist_ok=False)
        try:
            extension = output.suffix.lower()
            filename = f"kindle-{book_id[:10]}{extension}"
            final_file = final_dir / filename
            shutil.move(str(output), final_file)
            now = utc_now()
            record = {
                "id": book_id,
                "title": title,
                "author": author,
                "source_type": source_type,
                "source_url": source_url,
                "added_at": now,
                "filename": filename,
                "format": extension.lstrip(".").upper(),
                "size": final_file.stat().st_size,
                "status": "unread",
                "progress": 0,
                "started_at": None,
                "completed_at": None,
                "last_read_at": None,
                "updated_at": now,
                "download_count": 0,
                "last_downloaded_at": None,
                "removed_at": None,
            }
            with self._connect(write=True) as connection:
                connection.execute(
                    """
                    INSERT INTO books(
                        id, title, author, source_type, source_url, added_at,
                        filename, format, size, status, progress, updated_at,
                        download_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', 0, ?, 0)
                    """,
                    (
                        book_id,
                        title,
                        author,
                        source_type,
                        source_url,
                        now,
                        filename,
                        record["format"],
                        record["size"],
                        now,
                    ),
                )
                self._add_event(connection, book_id, "added", occurred_at=now)
        except Exception:
            shutil.rmtree(final_dir, ignore_errors=True)
            raise
        shutil.rmtree(work_dir, ignore_errors=True)
        return record

    def update_reading(
        self,
        book_id: str,
        *,
        status: str | None = None,
        progress: int | str | None = None,
        note: str = "",
    ) -> dict:
        if status is not None and status not in READING_STATUSES:
            raise LibraryError("未知的阅读状态。")
        parsed_progress: int | None = None
        if progress not in (None, ""):
            if isinstance(progress, bool) or not isinstance(progress, (int, str)):
                raise LibraryError("阅读进度必须是 0 到 100 的整数。")
            try:
                parsed_progress = int(progress)
            except (TypeError, ValueError) as exc:
                raise LibraryError("阅读进度必须是 0 到 100 的整数。") from exc
            if not 0 <= parsed_progress <= 100:
                raise LibraryError("阅读进度必须在 0 到 100 之间。")

        with self._connect(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM books WHERE id = ? AND removed_at IS NULL", (book_id,)
            ).fetchone()
            if not row:
                raise LibraryError("没有找到这本书。")

            now = utc_now()
            old_status = str(row["status"])
            old_progress = int(row["progress"])
            new_status = status or old_status
            new_progress = old_progress if parsed_progress is None else parsed_progress

            if status == "unread":
                new_progress = 0
            elif status == "finished":
                new_progress = 100
            elif status == "reading" and old_status == "finished":
                if parsed_progress in (None, 100):
                    # The desktop form submits the previous 100% alongside a
                    # changed status; treat that as a new reading cycle.
                    new_progress = 0
            elif status != "abandoned":
                if new_progress == 100:
                    new_status = "finished"
                elif parsed_progress is not None and (
                    old_status == "finished" or (old_status == "unread" and new_progress > 0)
                ):
                    new_status = "reading"

            if new_status == "unread":
                new_progress = 0

            started_at = row["started_at"]
            completed_at = row["completed_at"]
            if new_status == "unread":
                started_at = None
            elif new_status in {"reading", "finished"} and (
                not started_at or old_status == "finished" and new_status == "reading"
            ):
                started_at = now
            if new_status == "finished":
                completed_at = completed_at or now
            elif old_status == "finished":
                completed_at = None

            changed = old_status != new_status or old_progress != new_progress
            if not changed and not note.strip():
                return self._book_dict(row)

            connection.execute(
                """
                UPDATE books SET status = ?, progress = ?, started_at = ?,
                    completed_at = ?, last_read_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    new_progress,
                    started_at,
                    completed_at,
                    now,
                    now,
                    book_id,
                ),
            )
            if old_status != new_status:
                self._add_event(
                    connection,
                    book_id,
                    "status_changed",
                    old_status=old_status,
                    new_status=new_status,
                    old_progress=old_progress,
                    new_progress=new_progress,
                    note=note,
                    occurred_at=now,
                )
            if old_progress != new_progress:
                self._add_event(
                    connection,
                    book_id,
                    "progress_changed",
                    old_status=old_status,
                    new_status=new_status,
                    old_progress=old_progress,
                    new_progress=new_progress,
                    note=note,
                    occurred_at=now,
                )
            if not changed and note.strip():
                self._add_event(
                    connection,
                    book_id,
                    "note",
                    old_status=old_status,
                    new_status=new_status,
                    old_progress=old_progress,
                    new_progress=new_progress,
                    note=note,
                    occurred_at=now,
                )
            updated = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        return self._book_dict(updated)

    def record_download(self, book_id: str) -> None:
        with self._connect(write=True) as connection:
            row = connection.execute(
                "SELECT download_count FROM books WHERE id = ? AND removed_at IS NULL",
                (book_id,),
            ).fetchone()
            if not row:
                raise LibraryError("没有找到这本书。")
            now = utc_now()
            connection.execute(
                """
                UPDATE books SET download_count = download_count + 1,
                    last_downloaded_at = ?, updated_at = ? WHERE id = ?
                """,
                (now, now, book_id),
            )
            self._add_event(connection, book_id, "downloaded", occurred_at=now)

    def file_for(self, book_id: str, filename: str) -> Path | None:
        book = self.get(book_id)
        if not book or filename != book.get("filename"):
            return None
        if not _safe_path_component(filename):
            return None
        try:
            directory = self._book_directory(book_id)
            path = (directory / filename).resolve()
            if path.parent == directory and path.is_file():
                return path
        except (LibraryError, OSError, RuntimeError):
            return None
        return None

    def history(
        self,
        limit: int = 100,
        book_id: str = "",
        *,
        before_id: int | None = None,
    ) -> list[dict]:
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError, OverflowError) as exc:
            raise LibraryError("历史记录数量必须是整数。") from exc
        if before_id is not None and (
            isinstance(before_id, bool)
            or not isinstance(before_id, int)
            or not 1 <= before_id <= 2**63 - 1
        ):
            raise LibraryError("历史记录游标必须是 SQLite 范围内的正整数。")
        clauses: list[str] = []
        parameters: list[object] = []
        if book_id:
            clauses.append("e.book_id = ?")
            parameters.append(book_id)
        if before_id is not None:
            clauses.append("e.id < ?")
            parameters.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.*, b.title, b.author
                FROM reading_events e JOIN books b ON b.id = e.book_id
                {where}
                ORDER BY e.id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_history(self, book_id: str = "") -> Iterator[dict]:
        """Export every event in bounded batches, including removed books."""
        before_id = None
        while True:
            batch = self.history(500, book_id, before_id=before_id)
            if not batch:
                return
            yield from batch
            before_id = batch[-1]["id"]

    def statistics(self) -> dict:
        since_30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN")
            summary = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN status = 'unread' THEN 1 ELSE 0 END) AS unread,
                    SUM(CASE WHEN status = 'reading' THEN 1 ELSE 0 END) AS reading,
                    SUM(CASE WHEN status = 'finished' THEN 1 ELSE 0 END) AS finished,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) AS abandoned,
                    COALESCE(AVG(progress), 0) AS average_progress,
                    COALESCE(SUM(download_count), 0) AS downloads,
                    SUM(CASE WHEN added_at >= ? THEN 1 ELSE 0 END) AS added_30,
                    SUM(CASE WHEN completed_at >= ? THEN 1 ELSE 0 END) AS finished_30
                FROM books WHERE removed_at IS NULL
                """,
                (since_30, since_30),
            ).fetchone()
            recent_completed = connection.execute(
                """
                SELECT * FROM books
                WHERE removed_at IS NULL AND status = 'finished'
                ORDER BY completed_at DESC LIMIT 10
                """
            ).fetchall()
            monthly = connection.execute(
                """
                SELECT substr(occurred_at, 1, 7) AS month, COUNT(*) AS count
                FROM reading_events
                WHERE event_type = 'status_changed' AND new_status = 'finished'
                GROUP BY substr(occurred_at, 1, 7)
                ORDER BY month DESC LIMIT 12
                """
            ).fetchall()

        data = {key: (value or 0) for key, value in dict(summary).items()}
        denominator = int(data["total"]) - int(data["abandoned"])
        data["completion_rate"] = (
            round(int(data["finished"]) * 100 / denominator, 1) if denominator else 0.0
        )
        data["average_progress"] = round(float(data["average_progress"]), 1)
        data["recent_completed"] = [dict(row) for row in recent_completed]
        data["monthly_completed"] = [dict(row) for row in reversed(monthly)]
        return data

    def backup_to(self, destination: Path) -> Path:
        """Create a transactionally consistent online SQLite backup."""
        destination = destination.resolve()
        if destination in {
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        } or (destination.exists() and destination.samefile(self.database_path)):
            raise LibraryError("备份不能覆盖正在使用的数据库或其日志文件。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self._connect() as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                target.commit()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def remove(self, book_id: str) -> bool:
        with self._file_lock:
            source = None
            destination = None
            try:
                with self._connect(write=True) as connection:
                    book = connection.execute(
                        "SELECT * FROM books WHERE id = ? AND removed_at IS NULL",
                        (book_id,),
                    ).fetchone()
                    if not book:
                        return False
                    source = self._book_directory(book_id)
                    self._validate_storage_directory(self.trash_dir)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    destination = self.trash_dir / (f"{book_id}-{stamp}-{uuid.uuid4().hex[:8]}")
                    if source.exists():
                        shutil.move(str(source), destination)
                    now = utc_now()
                    connection.execute(
                        "UPDATE books SET removed_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, book_id),
                    )
                    self._add_event(connection, book_id, "removed", occurred_at=now)
            except Exception:
                if destination is not None and destination.exists():
                    shutil.move(str(destination), source)
                raise
            return True
