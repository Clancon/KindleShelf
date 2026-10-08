from io import BytesIO
from pathlib import Path

from kindle_shelf.articles import ArticleExtractor
from kindle_shelf.library import ShelfLibrary


class FakeConverter:
    available = True

    def convert(self, source: Path, destination: Path, **_kwargs):
        destination.write_bytes(b"FAKE-MOBI:" + source.read_bytes())


def test_upload_epub_converts_and_can_be_resolved(tmp_path):
    library = ShelfLibrary(tmp_path, FakeConverter(), ArticleExtractor())
    book = library.add_upload(BytesIO(b"epub-data"), "demo.epub", title="测试书")
    assert book["title"] == "测试书"
    assert book["format"] == "MOBI"
    assert library.file_for(book["id"], book["filename"]).read_bytes().startswith(b"FAKE-MOBI")


def test_native_pdf_is_kept_in_auto_mode(tmp_path):
    library = ShelfLibrary(tmp_path, FakeConverter(), ArticleExtractor())
    book = library.add_upload(BytesIO(b"%PDF-test"), "paper.pdf")
    assert book["format"] == "PDF"
    assert library.file_for(book["id"], book["filename"]).read_bytes() == b"%PDF-test"


def test_remove_moves_book_to_trash(tmp_path):
    library = ShelfLibrary(tmp_path, FakeConverter(), ArticleExtractor())
    book = library.add_upload(BytesIO(b"book"), "book.mobi")
    assert library.remove(book["id"])
    assert library.get(book["id"]) is None
    assert list((tmp_path / ".trash").iterdir())
