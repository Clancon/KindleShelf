import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from kindle_shelf.converter import CalibreConverter, ConversionError


@pytest.fixture
def converter(tmp_path):
    executable = tmp_path / "ebook-convert"
    executable.write_bytes(b"fake executable")
    return CalibreConverter(executable)


def test_conversion_failure_does_not_leak_paths_or_replace_existing_book(
    tmp_path, converter, monkeypatch
):
    source = tmp_path / "source.epub"
    source.write_bytes(b"invalid ebook")
    destination = tmp_path / "book.mobi"
    destination.write_bytes(b"original book")

    def run(command, **kwargs):
        Path(command[2]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, stderr=f"private token=secret in {source}")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ConversionError) as error:
        converter.convert(source, destination, title="Title")
    assert str(tmp_path) not in str(error.value)
    assert "secret" not in str(error.value)
    assert destination.read_bytes() == b"original book"
    assert not list(tmp_path.glob(".conversion-*"))


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("fake command /private/token", 300),
        OSError("cannot execute /private/token"),
    ],
)
def test_conversion_process_errors_are_safe_and_cleaned(tmp_path, converter, monkeypatch, failure):
    source = tmp_path / "source.txt"
    source.write_text("Readable text", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(ConversionError) as error:
        converter.convert(source, tmp_path / "book.mobi", title="Title")
    assert "/private/token" not in str(error.value)
    assert not list(tmp_path.glob(".conversion-*"))


def test_empty_successful_output_is_rejected(tmp_path, converter, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("Readable text", encoding="utf-8")

    def run(command, **kwargs):
        Path(command[2]).touch()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ConversionError):
        converter.convert(source, tmp_path / "book.mobi", title="Title")
    assert not (tmp_path / "book.mobi").exists()


def test_html_resources_are_sanitized_before_calibre(tmp_path, converter, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "private.png"
    png = b"\x89PNG\r\n\x1a\n" + b"image bytes"
    outside.write_bytes(png)
    (work / "local.png").write_bytes(png)
    source = work / "source.html"
    source.write_text(
        """<html><head><base href="file:///private/">
        <link rel="stylesheet" href="http://127.0.0.1/secret">
        <style>@import "http://127.0.0.1/secret";</style></head>
        <body onload="bad()"><p>Readable content.</p>
        <a href="../private.html">Secret linked file</a>
        <a href="https://example.com/next">A public link</a>
        <img src="../private.png">
        <img src="file:///private/secret.png">
        <img src="http://127.0.0.1/secret.png">
        <img src="local.png">
        <object data="file:///private/secret">object</object>
        </body></html>""",
        encoding="utf-8",
    )
    observed = {}

    def run(command, **kwargs):
        prepared = Path(command[1])
        document = prepared.read_text(encoding="utf-8")
        soup = BeautifulSoup(document, "html.parser")
        assert not soup.find(["object", "script", "link", "base"])
        assert "127.0.0.1" not in document and "private.png" not in document
        assert "@import" not in document and "onload" not in document
        images = soup.find_all("img")
        assert len(images) == 1
        assert (prepared.parent / images[0]["src"]).read_bytes() == png
        assert "https://example.com/next" in document
        assert command[-2:] == ["--max-levels", "0"]
        assert "--allow-local-files-outside-root" not in command
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 300
        observed["prepared"] = prepared
        Path(command[2]).write_bytes(b"converted MOBI")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    destination = work / "book.mobi"
    converter.convert(source, destination, title="Safe HTML")
    assert destination.read_bytes() == b"converted MOBI"
    assert not observed["prepared"].exists()
    assert outside.read_bytes() == png


def test_html_resource_symlink_cannot_escape_source_directory(tmp_path, converter, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "private.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    link = root / "linked.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("OS does not allow creating symlinks")
    source = root / "source.html"
    source.write_text('<p>Readable article content.</p><img src="linked.png">', encoding="utf-8")

    def run(command, **kwargs):
        soup = BeautifulSoup(Path(command[1]).read_text(encoding="utf-8"), "html.parser")
        assert soup.find("img") is None
        Path(command[2]).write_bytes(b"converted MOBI")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    converter.convert(source, root / "book.mobi", title="Safe HTML")
