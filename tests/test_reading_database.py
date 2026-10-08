import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest

from kindle_shelf.articles import ArticleExtractor
from kindle_shelf.library import LibraryError, ShelfLibrary


class FakeConverter:
    available = True

    def convert(self, source: Path, destination: Path, **_kwargs):
        destination.write_bytes(b"MOBI" + source.read_bytes())


def make_library(path: Path) -> ShelfLibrary:
    return ShelfLibrary(path, FakeConverter(), ArticleExtractor())


def add_book(library: ShelfLibrary, title: str) -> dict:
    return library.add_upload(BytesIO(b"test-book"), f"{title}.mobi", title=title)


def test_database_persists_books_and_reading_history(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library, "持久化测试")
    library.update_reading(book["id"], progress=42, note="读到第四章")

    reopened = make_library(tmp_path)
    saved = reopened.get(book["id"])
    assert saved["status"] == "reading"
    assert saved["progress"] == 42
    events = reopened.history(book_id=book["id"])
    assert {event["event_type"] for event in events} >= {
        "added",
        "status_changed",
        "progress_changed",
    }
    assert any(event["note"] == "读到第四章" for event in events)


def test_status_rules_and_statistics(tmp_path):
    library = make_library(tmp_path)
    reading = add_book(library, "在读书")
    finished = add_book(library, "完成书")
    abandoned = add_book(library, "搁置书")

    library.update_reading(reading["id"], progress=35)
    library.update_reading(finished["id"], status="finished")
    library.update_reading(abandoned["id"], status="abandoned")

    stats = library.statistics()
    assert stats["total"] == 3
    assert stats["unread"] == 0
    assert stats["reading"] == 1
    assert stats["finished"] == 1
    assert stats["abandoned"] == 1
    assert stats["average_progress"] == 45.0
    assert stats["completion_rate"] == 50.0
    assert stats["finished_30"] == 1
    assert stats["monthly_completed"][-1]["count"] == 1


def test_finishing_and_restarting_normalizes_progress(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library, "重读书")
    finished = library.update_reading(book["id"], progress=100)
    assert finished["status"] == "finished"
    assert finished["completed_at"]

    restarted = library.update_reading(book["id"], status="reading")
    assert restarted["status"] == "reading"
    assert restarted["progress"] == 0
    assert restarted["completed_at"] is None


@pytest.mark.parametrize("progress", [-1, 101, "abc"])
def test_invalid_progress_is_rejected(tmp_path, progress):
    library = make_library(tmp_path)
    book = add_book(library, "验证进度")
    with pytest.raises(LibraryError):
        library.update_reading(book["id"], progress=progress)


def test_download_counts_are_atomic_under_concurrency(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library, "下载计数")
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _index: library.record_download(book["id"]), range(12)))
    assert library.get(book["id"])["download_count"] == 12
    assert sum(event["event_type"] == "downloaded" for event in library.history(50)) == 12


def test_remove_is_soft_delete_and_preserves_history(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library, "保留历史")
    library.update_reading(book["id"], status="finished")
    assert library.remove(book["id"])

    assert library.get(book["id"]) is None
    removed = library.get(book["id"], include_removed=True)
    assert removed["removed_at"]
    assert library.statistics()["total"] == 0
    assert library.history(book_id=book["id"])[0]["event_type"] == "removed"
    assert list((tmp_path / ".trash").iterdir())


def test_legacy_json_is_migrated_once(tmp_path):
    book_id = "legacy123"
    book_dir = tmp_path / "books" / book_id
    book_dir.mkdir(parents=True)
    (book_dir / "kindle-legacy.mobi").write_bytes(b"legacy")
    legacy = [
        {
            "id": book_id,
            "title": "旧版书目",
            "author": "旧作者",
            "source_type": "upload",
            "source_url": "",
            "added_at": "2025-01-02T03:04:05+00:00",
            "filename": "kindle-legacy.mobi",
            "format": "MOBI",
            "size": 6,
        }
    ]
    (tmp_path / "library.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    library = make_library(tmp_path)
    assert library.get(book_id)["title"] == "旧版书目"
    assert library.history(book_id=book_id)[0]["event_type"] == "migrated"
    assert len(make_library(tmp_path).list()) == 1


def test_online_database_backup_is_consistent(tmp_path):
    library = make_library(tmp_path / "live")
    book = add_book(library, "备份测试")
    library.update_reading(book["id"], progress=66)
    backup = library.backup_to(tmp_path / "backup" / "shelf.db")

    with sqlite3.connect(backup) as connection:
        saved = connection.execute(
            "SELECT title, status, progress FROM books WHERE id = ?", (book["id"],)
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM reading_events WHERE book_id = ?", (book["id"],)
        ).fetchone()[0]
    assert saved == ("备份测试", "reading", 66)
    assert event_count == 3
