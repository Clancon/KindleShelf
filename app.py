from __future__ import annotations

import argparse
import os
import threading
import webbrowser

from waitress import serve

from kindle_shelf import create_app
from kindle_shelf.version import __version__
from kindle_shelf.web import local_addresses


def main() -> None:
    parser = argparse.ArgumentParser(description="Kindle Shelf local delivery server")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--host",
        default=os.environ.get("KINDLE_SHELF_HOST", "0.0.0.0"),
        help="listen address",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("KINDLE_SHELF_PORT", "8090")),
        help="listen port",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open the PC page")
    args = parser.parse_args()

    app = create_app({"PORT": args.port})
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()

    print(f"Kindle Shelf {__version__} 已启动。")
    print(f"电脑管理页：http://127.0.0.1:{args.port}/")
    addresses = (
        [f"{app.config['PUBLIC_URL']}/kindle"]
        if app.config["PUBLIC_URL"]
        else local_addresses(args.port)
    )
    for address in addresses:
        print(f"Kindle 书架：{address}")
    print("按 Ctrl+C 停止。")
    serve(app, host=args.host, port=args.port, threads=6)


if __name__ == "__main__":
    main()
