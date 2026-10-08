from pathlib import Path

import pytest

from kindle_shelf.converter import CalibreConverter
from kindle_shelf.web import create_app


def test_real_calibre_creates_mobi(tmp_path: Path):
    converter = CalibreConverter()
    if not converter.available:
        pytest.skip("Calibre is not installed")

    source = tmp_path / "article.html"
    source.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>测试文章</title></head>"
        "<body><h1>测试文章</h1><p>这是一段用于验证真实转换流程的中文正文。</p></body></html>",
        encoding="utf-8",
    )
    output = tmp_path / "article.mobi"
    converter.convert(source, output, title="测试文章", author="Kindle Shelf")

    assert output.is_file()
    assert output.stat().st_size > 1000
    assert output.read_bytes()[60:68] == b"BOOKMOBI"


def test_real_end_to_end_article_reading_and_download(tmp_path: Path, post_form):
    converter = CalibreConverter()
    if not converter.available:
        pytest.skip("Calibre is not installed")

    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path,
            "CONVERTER": converter,
            "SECRET_KEY": "integration-test",
        }
    )
    client = app.test_client()
    created = post_form(
        client,
        "/add/text",
        data={
            "title": "真实端到端文章",
            "author": "测试作者",
            "text": "第一段中文正文。\n\n第二段用于验证 MOBI 生成和下载。",
        },
        follow_redirects=True,
    )
    assert created.status_code == 200
    library = app.extensions["shelf_library"]
    book = library.list()[0]
    assert library.file_for(book["id"], book["filename"]).stat().st_size > 1000

    completed = post_form(
        client,
        f"/reading/{book['id']}",
        data={"status": "finished", "return_to": "index"},
        follow_redirects=True,
    )
    assert completed.status_code == 200
    downloaded = client.get(f"/download/{book['id']}/{book['filename']}")
    assert downloaded.status_code == 200
    assert len(downloaded.data) > 1000

    stats = client.get("/api/stats").get_json()
    assert stats["finished"] == 1
    assert stats["downloads"] == 1
    assert stats["completion_rate"] == 100.0
