"""Container readiness check, including the database and Calibre executable."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def check_health(base_url: str, timeout: float = 3.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{base_url.rstrip('/')}/health", timeout=timeout) as response:
            if response.status != 200:
                return False
            result = json.load(response)
    except (OSError, urllib.error.URLError, ValueError):
        return False
    return isinstance(result, dict) and all(
        result.get(key) is True for key in ("ok", "database", "calibre")
    )


def main() -> int:
    port = os.environ.get("KINDLE_SHELF_PORT", "8090")
    if not port.isdigit() or not 0 < int(port) <= 65535:
        return 1
    return 0 if check_health(f"http://127.0.0.1:{port}") else 1


if __name__ == "__main__":
    sys.exit(main())
