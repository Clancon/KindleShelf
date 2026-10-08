import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO

import pytest

from kindle_shelf import library as library_module
from kindle_shelf.articles import ArticleExtractor
from kindle_shelf.library import LibraryError, ShelfLibrary


class FakeConverter:
    available = True

    def convert(self, source, destination, **_kwargs):
        destination.write_bytes(b"MOBI" + source.read_bytes())


def make_library(path):
    return ShelfLibrary(path, FakeConverter(), ArticleExtractor())


def add_book(library, title="Regression book"):
    return library.add_upload(BytesIO(b"test-book"), "book.mobi", title=title)


@pytest.mark.parametrize(
    ("status", "progress", "expected_status", "expected_progress"),
    [
        ("unread", "100", "unread", 0),
        ("reading", "100", "reading", 0),
        ("reading", None, "reading", 0),
        ("reading", "20", "reading", 20),
        ("abandoned", "100", "abandoned", 100),
        (None, "25", "reading", 25),
    ],
)
def test_finished_book_can_be_reopened_from_form(
    tmp_path, status, progress, expected_status, expected_progress
):
    library = make_library(tmp_path)
    book = add_book(library)
    library.update_reading(book["id"], status="finished")

    reopened = library.update_reading(book["id"], status=status, progress=progress)

    assert reopened["status"] == expected_status
    assert reopened["progress"] == expected_progress
    assert reopened["completed_at"] is None
    if expected_status == "unread":
        assert reopened["started_at"] is None
    # Previous completion remains available for the user's retrospective.
    assert any(event["new_status"] == "finished" for event in library.history(book_id=book["id"]))


@pytest.mark.parametrize("progress", [25.5, True, False, b"12", "12.0", float("inf"), float("nan")])
def test_progress_rejects_non_integer_types(tmp_path, progress):
    library = make_library(tmp_path)
    book = add_book(library)
    before = library.get(book["id"])
    with pytest.raises(LibraryError):
        library.update_reading(book["id"], progress=progress)
    assert library.get(book["id"]) == before
    assert len(library.history(book_id=book["id"])) == 1


def test_database_connections_are_closed_on_success_and_failure(tmp_path, monkeypatch):
    original_connect = sqlite3.connect
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(library_module.sqlite3, "connect", tracked_connect)
    library = make_library(tmp_path / "live")
    book = add_book(library)
    library.list()
    library.get(book["id"])
    library.update_reading(book["id"], progress=20)
    library.update_reading(book["id"], progress=20)  # No-op early return.
    library.record_download(book["id"])
    library.history()
    library.statistics()
    library.backup_to(tmp_path / "backup.db")
    with pytest.raises(LibraryError):
        library.update_reading("missing", status="finished")
    library.remove(book["id"])

    assert connections
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("version", ["2", "invalid", "-1"])
def test_incompatible_schema_is_not_overwritten(tmp_path, version):
    database = tmp_path / "kindle_shelf.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (version,),
        )

    with pytest.raises(LibraryError):
        make_library(tmp_path)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == version
        )
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'books'").fetchone()
            is None
        )


@pytest.mark.parametrize(
    ("book_id", "filename", "size"),
    [
        ("../outside", "book.mobi", 6),
        ("safe", "../outside.mobi", 6),
        ("safe", r"..\outside.mobi", 6),
        ("safe", "C:outside.mobi", 6),
        ("safe", "book.mobi", -1),
        ("safe", "book.mobi", "invalid"),
    ],
)
def test_invalid_legacy_records_do_not_partially_migrate(tmp_path, book_id, filename, size):
    records = [
        {"id": "valid", "filename": "valid.mobi", "size": 6},
        {"id": book_id, "filename": filename, "size": size},
    ]
    index = tmp_path / "library.json"
    payload = json.dumps(records)
    index.write_text(payload, encoding="utf-8")
    with pytest.raises(LibraryError):
        make_library(tmp_path)

    assert index.read_text(encoding="utf-8") == payload
    with sqlite3.connect(tmp_path / "kindle_shelf.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'legacy_json_migrated'"
            ).fetchone()
            is None
        )


def test_symbolic_book_directory_cannot_escape_data_directory(tmp_path):
    library = make_library(tmp_path / "live")
    book = add_book(library)
    external = tmp_path / "external"
    external.mkdir()
    protected = external / book["filename"]
    protected.write_bytes(b"private external file")
    directory = library.books_dir / book["id"]
    (directory / book["filename"]).unlink()
    directory.rmdir()
    try:
        directory.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating directory symlinks is unavailable on this platform")

    assert library.file_for(book["id"], book["filename"]) is None
    with pytest.raises(LibraryError):
        library.remove(book["id"])
    assert protected.read_bytes() == b"private external file"
    assert library.get(book["id"]) is not None


def test_failed_insert_cleans_up_staged_and_final_book_files(tmp_path, monkeypatch):
    library = make_library(tmp_path)

    def fail_event(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated transaction failure")

    monkeypatch.setattr(library, "_add_event", fail_event)
    with pytest.raises(sqlite3.OperationalError):
        add_book(library)

    assert library.list() == []
    assert list(library.books_dir.iterdir()) == []
    assert list(library.temp_dir.iterdir()) == []


def test_failed_file_move_cleans_up_empty_final_directory(tmp_path, monkeypatch):
    library = make_library(tmp_path)

    def fail_move(*_args, **_kwargs):
        raise OSError("simulated file move failure")

    monkeypatch.setattr(library_module.shutil, "move", fail_move)
    with pytest.raises(OSError):
        add_book(library)

    assert library.list() == []
    assert list(library.books_dir.iterdir()) == []
    assert list(library.temp_dir.iterdir()) == []


def test_failed_remove_restores_file_and_rolls_back_history(tmp_path, monkeypatch):
    library = make_library(tmp_path)
    book = add_book(library)
    before = library.get(book["id"])

    def fail_event(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated history failure")

    monkeypatch.setattr(library, "_add_event", fail_event)
    with pytest.raises(sqlite3.OperationalError):
        library.remove(book["id"])

    assert library.get(book["id"]) == before
    assert library.file_for(book["id"], book["filename"]).read_bytes() == b"test-book"
    assert list(library.trash_dir.iterdir()) == []
    assert len(library.history(book_id=book["id"])) == 1


def test_failed_remove_commit_restores_book_file(tmp_path, monkeypatch):
    library = make_library(tmp_path)
    book = add_book(library)
    original_connect = sqlite3.connect

    class FailingCommitConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            if exc_type is None and self.in_transaction:
                self.rollback()
                raise sqlite3.OperationalError("simulated commit failure")
            return super().__exit__(exc_type, exc_value, traceback)

    def failing_connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=FailingCommitConnection)

    monkeypatch.setattr(library_module.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError):
        library.remove(book["id"])

    assert library.get(book["id"]) is not None
    assert library.file_for(book["id"], book["filename"]).read_bytes() == b"test-book"
    assert list(library.trash_dir.iterdir()) == []
    assert len(library.history(book_id=book["id"])) == 1


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_backup_cannot_overwrite_live_database_or_sidecars(tmp_path, suffix):
    library = make_library(tmp_path)
    book = add_book(library)
    destination = library.database_path.with_name(library.database_path.name + suffix)

    with pytest.raises(LibraryError):
        library.backup_to(destination)

    assert library.get(book["id"]) is not None
    with sqlite3.connect(library.database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_cannot_overwrite_hard_link_to_live_database(tmp_path):
    library = make_library(tmp_path)
    destination = tmp_path / "linked.db"
    try:
        destination.hardlink_to(library.database_path)
    except (OSError, NotImplementedError):
        pytest.skip("Creating hard links is unavailable on this platform")

    with pytest.raises(LibraryError):
        library.backup_to(destination)


def test_failed_backup_keeps_previous_backup_and_removes_staging_file(tmp_path, monkeypatch):
    library = make_library(tmp_path / "live")
    book = add_book(library)
    destination = tmp_path / "saved.db"
    library.backup_to(destination)
    before = destination.read_bytes()

    class FailingSource:
        def backup(self, _target):
            raise sqlite3.OperationalError("simulated backup failure")

    @contextmanager
    def failing_connection():
        yield FailingSource()

    monkeypatch.setattr(library, "_connect", failing_connection)
    with pytest.raises(sqlite3.OperationalError):
        library.backup_to(destination)

    assert destination.read_bytes() == before
    assert list(tmp_path.glob(".saved.db.*.tmp")) == []
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT id FROM books").fetchone()[0] == book["id"]


def test_concurrent_reading_updates_keep_a_consistent_event_chain(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(
            pool.map(
                lambda progress: library.update_reading(
                    book["id"], progress=progress, note=f"update-{progress}"
                ),
                range(1, 31),
            )
        )

    changes = [
        event
        for event in reversed(library.history(500, book_id=book["id"]))
        if event["event_type"] == "progress_changed"
    ]
    assert len(changes) == 30
    previous = 0
    for event in changes:
        assert event["old_progress"] == previous
        previous = event["new_progress"]
    assert library.get(book["id"])["progress"] == previous


def test_history_cursor_and_export_preserve_events_beyond_one_page(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library)
    for index in range(505):
        library.update_reading(book["id"], note=f"note-{index}")

    first = library.history(500, book_id=book["id"])
    second = library.history(500, book_id=book["id"], before_id=first[-1]["id"])
    assert len(first) == 500
    assert len(second) == 6
    assert not ({event["id"] for event in first} & {event["id"] for event in second})
    exported = list(library.iter_history(book_id=book["id"]))
    assert exported == first + second
    assert library.history(500, before_id=second[-1]["id"]) == []


@pytest.mark.parametrize("before_id", [0, -1, "invalid", True, 1.5, 2**63])
def test_invalid_history_cursor_is_rejected(tmp_path, before_id):
    library = make_library(tmp_path)
    with pytest.raises(LibraryError):
        library.history(before_id=before_id)


def test_history_cursor_accepts_sqlite_upper_boundary(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library)
    assert library.history(before_id=2**63 - 1)[0]["book_id"] == book["id"]


def test_removing_read_book_keeps_historical_completions(tmp_path):
    library = make_library(tmp_path)
    book = add_book(library)
    library.update_reading(book["id"], status="finished")
    library.update_reading(book["id"], status="reading")
    library.update_reading(book["id"], status="finished")
    library.remove(book["id"])

    statistics = library.statistics()
    assert statistics["total"] == 0
    assert statistics["finished"] == 0
    assert statistics["monthly_completed"][-1]["count"] == 2
    assert len(list(library.iter_history(book["id"]))) >= 7
