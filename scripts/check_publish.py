"""Check the Git index, without printing any credential values."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import PurePosixPath

PRIVATE_DIRECTORIES = {
    "data",
    "logs",
    "backups",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".git",
    ".ssh",
}
PRIVATE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".epub",
    ".mobi",
    ".azw",
    ".azw3",
    ".pdf",
    ".docx",
    ".pyo",
    ".pyc",
    ".cbz",
    ".rtf",
}
CREDENTIAL_PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
]


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        any(part in PRIVATE_DIRECTORIES or part.startswith(".venv") for part in path.parts)
        or (path.name.startswith(".env") and path.name != ".env.example")
        or path.name in {".session-secret", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
        or path.suffix.lower() in PRIVATE_SUFFIXES
        or ".db-" in path.name
        or ".sqlite-" in path.name
        or ".sqlite3-" in path.name
    )


def main() -> int:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "-z"],
            check=True,
            capture_output=True,
        )
        files = [name.decode("utf-8") for name in result.stdout.split(b"\x00") if name]
        problems = []
        for name in files:
            if private_path(name):
                problems.append(f"private data or key file in index: {name}")
                continue
            content = subprocess.run(
                ["git", "show", f":{name}"], check=True, capture_output=True
            ).stdout
            if any(pattern.search(content) for pattern in CREDENTIAL_PATTERNS):
                problems.append(f"credential-like content in index: {name}")
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        print("Unable to inspect the Git index.", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(
        f"Publication check passed: {len(files)} indexed files; no private paths or known key patterns."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
