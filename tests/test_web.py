from pathlib import Path

from kindle_shelf.web import create_app


class FakeConverter:
    available = True

    def convert(self, source: Path, destination: Path, **_kwargs):
        destination.write_bytes(b"MOBI" + source.read_bytes())


def test_text_to_kindle_download_flow(tmp_path, post_form):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path,
            "CONVERTER": FakeConverter(),
            "SECRET_KEY": "test",
        }
    )
    client = app.test_client()
    response = post_form(
        client,
        "/add/text",
        data={"title": "离线文章", "author": "作者", "text": "第一段\n\n第二段"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "离线文章" in response.get_data(as_text=True)

    kindle = client.get("/kindle")
    assert kindle.status_code == 200
    assert "下载到 Kindle" in kindle.get_data(as_text=True)

    book = app.extensions["shelf_library"].list()[0]
    head = client.head(f"/download/{book['id']}/{book['filename']}")
    assert head.status_code == 200
    assert app.extensions["shelf_library"].get(book["id"])["download_count"] == 0
    download = client.get(f"/download/{book['id']}/{book['filename']}")
    assert download.status_code == 200
    assert download.data.startswith(b"MOBI")
    assert app.extensions["shelf_library"].get(book["id"])["download_count"] == 1
    resumed = client.get(
        f"/download/{book['id']}/{book['filename']}", headers={"Range": "bytes=0-3"}
    )
    assert resumed.status_code == 206
    assert app.extensions["shelf_library"].get(book["id"])["download_count"] == 1


def test_reading_update_stats_history_and_filters(tmp_path, post_form):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path,
            "CONVERTER": FakeConverter(),
            "SECRET_KEY": "test",
        }
    )
    library = app.extensions["shelf_library"]
    book = library.add_text("统计文章", "作者", "正文内容足够用于测试")
    client = app.test_client()

    response = post_form(
        client,
        f"/reading/{book['id']}",
        data={"status": "reading", "progress": "60", "note": "读到一半以后"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "进度 60%" in response.get_data(as_text=True)

    stats_page = client.get("/stats")
    body = stats_page.get_data(as_text=True)
    assert stats_page.status_code == 200
    assert "阅读统计与回溯" in body
    assert "读到一半以后" in body

    stats = client.get("/api/stats").get_json()
    assert stats["reading"] == 1
    assert stats["average_progress"] == 60.0
    history = client.get("/api/history").get_json()["events"]
    assert any(event["event_type"] == "progress_changed" for event in history)

    kindle = client.get("/kindle?status=reading")
    assert "统计文章" in kindle.get_data(as_text=True)
    assert "进度 60%" in kindle.get_data(as_text=True)


def test_health_reports_database(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path,
            "CONVERTER": FakeConverter(),
        }
    )
    health = app.test_client().get("/health").get_json()
    assert health["ok"] is True
    assert health["calibre"] is True
    assert health["database"] is True
    assert health["books"] == 0
    assert health["version"] == "0.2.0"


def test_database_backup_download(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path, "CONVERTER": FakeConverter()})
    app.extensions["shelf_library"].add_text("备份文章", "", "正文")
    response = app.test_client().get("/backup/database")
    assert response.status_code == 200
    assert response.data.startswith(b"SQLite format 3\x00")
    assert "attachment" in response.headers["Content-Disposition"]
