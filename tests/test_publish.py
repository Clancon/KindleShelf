import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_publish", Path(__file__).resolve().parents[1] / "scripts" / "check_publish.py"
)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


@pytest.mark.parametrize(
    "path",
    [
        "data/kindle_shelf.db",
        "logs/server.out.log",
        ".env",
        ".env.production",
        "backups/snapshot.db",
        "private.pem",
        ".session-secret",
        "book.epub",
        "kindle_shelf/__pycache__/web.pyc",
        ".venv/Scripts/python.exe",
        "backups/complete-data.tar.gz",
        ".ssh/config",
        "id_rsa",
        "book.cbz",
        "book.rtf",
    ],
)
def test_private_publication_paths_are_rejected(path):
    assert publish.private_path(path)


@pytest.mark.parametrize(
    "path",
    [".env.example", "kindle_shelf/web.py", "tests/test_web.py", "docs/REVIEW.md", "LICENSE"],
)
def test_source_and_blank_config_template_are_publishable(path):
    assert not publish.private_path(path)


def test_known_credential_patterns_detect_without_echoing_values():
    token = b"ghp_" + b"A" * 36
    assert any(pattern.search(token) for pattern in publish.CREDENTIAL_PATTERNS)
