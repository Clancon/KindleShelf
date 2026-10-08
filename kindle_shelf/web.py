from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)

from .articles import ArticleError, ArticleExtractor
from .converter import CalibreConverter, ConversionError
from .library import LibraryError, ShelfLibrary
from .security import csrf_token, protect_form, public_base_url, session_secret
from .version import __version__

STATUS_LABELS = {
    "unread": "未读",
    "reading": "在读",
    "finished": "已完成",
    "abandoned": "暂不继续",
}
EVENT_LABELS = {
    "added": "加入书架",
    "migrated": "迁入数据库",
    "downloaded": "下载到设备",
    "status_changed": "变更状态",
    "progress_changed": "更新进度",
    "note": "添加记录",
    "removed": "移出书架",
}


def local_addresses(port: int) -> list[str]:
    addresses: set[str] = set()
    primary: str | None = None
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127.") and not address.startswith("169.254."):
                addresses.add(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        primary = probe.getsockname()[0]
        addresses.add(primary)
        probe.close()
    except OSError:
        pass
    # The address selected by the default route is almost always the active
    # Wi-Fi/Ethernet adapter. Showing every virtual adapter is confusing on
    # Windows machines with WSL, VPNs or Hyper-V installed.
    if primary and not primary.startswith(("127.", "169.254.")):
        return [f"http://{primary}:{port}/kindle"]
    return [f"http://{address}:{port}/kindle" for address in sorted(addresses)]


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=os.environ.get("KINDLE_SHELF_SECRET", ""),
        MAX_CONTENT_LENGTH=200 * 1024 * 1024,
        DATA_DIR=Path(
            os.environ.get("KINDLE_SHELF_DATA", Path(__file__).resolve().parents[1] / "data")
        ),
        PORT=int(os.environ.get("KINDLE_SHELF_PORT", "8090")),
        PUBLIC_URL=os.environ.get("KINDLE_SHELF_PUBLIC_URL", ""),
        DISPLAY_TIMEZONE=os.environ.get("KINDLE_SHELF_TIMEZONE", "Asia/Shanghai"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_FORM_MEMORY_SIZE=20 * 1024 * 1024,
    )
    if config:
        app.config.update(config)

    app.config["DATA_DIR"] = Path(app.config["DATA_DIR"]).resolve()
    app.config["PUBLIC_URL"] = public_base_url(app.config["PUBLIC_URL"])
    app.config["SECRET_KEY"] = session_secret(app.config["DATA_DIR"], app.config["SECRET_KEY"])
    try:
        display_zone = ZoneInfo(app.config["DISPLAY_TIMEZONE"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError("KINDLE_SHELF_TIMEZONE 不是有效的 IANA 时区。") from exc
    if not app.config.get("TRUSTED_HOSTS"):
        app.config["TRUSTED_HOSTS"] = [
            "localhost",
            "127.0.0.1",
            "[::1]",
            *[urlsplit(url).hostname for url in local_addresses(int(app.config["PORT"]))],
            *[
                host.strip()
                for host in os.environ.get("KINDLE_SHELF_TRUSTED_HOSTS", "").split(",")
                if host.strip()
            ],
        ]
        if app.config["PUBLIC_URL"]:
            app.config["TRUSTED_HOSTS"].append(urlsplit(app.config["PUBLIC_URL"]).hostname)

    converter = app.config.get("CONVERTER") or CalibreConverter()
    extractor = app.config.get("ARTICLE_EXTRACTOR") or ArticleExtractor()
    library = ShelfLibrary(Path(app.config["DATA_DIR"]), converter, extractor)
    app.extensions["shelf_library"] = library
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def verify_form():
        protect_form(app.config["PUBLIC_URL"])

    @app.after_request
    def private_response(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def display_time(value: str | None, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).astimezone(display_zone).strftime(pattern)
        except (TypeError, ValueError):
            return str(value)

    app.jinja_env.filters["filesize"] = human_size
    app.jinja_env.filters["localtime"] = display_time

    def source_link(value: str) -> str:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
            ):
                return value
        except ValueError:
            pass
        return ""

    app.jinja_env.filters["source_link"] = source_link
    app.jinja_env.filters["status_label"] = lambda value: STATUS_LABELS.get(value, value)
    app.jinja_env.filters["event_label"] = lambda value: EVENT_LABELS.get(value, value)

    @app.context_processor
    def shared_values() -> dict:
        return {
            "calibre_available": converter.available,
            "kindle_addresses": (
                [f"{app.config['PUBLIC_URL']}/kindle"]
                if app.config["PUBLIC_URL"]
                else local_addresses(int(app.config["PORT"]))
            ),
            "reading_statuses": STATUS_LABELS,
            "app_version": __version__,
            "display_timezone": app.config["DISPLAY_TIMEZONE"],
        }

    @app.get("/")
    def index():
        query = request.args.get("q", "").strip()
        status = request.args.get("status", "").strip()
        try:
            books = library.list(query, status)
        except LibraryError:
            status = ""
            books = library.list(query)
        return render_template(
            "index.html",
            books=books,
            query=query,
            selected_status=status,
            stats=library.statistics(),
        )

    @app.post("/add/upload")
    def add_upload():
        uploaded = request.files.get("book")
        if not uploaded or not uploaded.filename:
            flash("请选择要上传的书籍。", "error")
            return redirect(url_for("index"))
        try:
            book = library.add_upload(
                uploaded.stream,
                uploaded.filename,
                title=request.form.get("title", ""),
                author=request.form.get("author", ""),
                target=request.form.get("target", "auto"),
            )
            flash(f"《{book['title']}》已加入 Kindle 书架。", "success")
        except (LibraryError, ConversionError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/add/url")
    def add_url():
        url = request.form.get("url", "").strip()
        try:
            article = extractor.fetch(url)
            override_title = request.form.get("title", "").strip()
            if override_title:
                article = type(article)(
                    url=article.url,
                    title=override_title,
                    author=article.author,
                    content_html=article.content_html,
                )
            book = library.add_article(article, request.form.get("target", "mobi"))
            flash(f"《{book['title']}》已抓取并转换完成。", "success")
        except (ArticleError, LibraryError, ConversionError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/add/text")
    def add_text():
        try:
            book = library.add_text(
                request.form.get("title", ""),
                request.form.get("author", ""),
                request.form.get("text", ""),
                request.form.get("target", "mobi"),
            )
            flash(f"《{book['title']}》已转换完成。", "success")
        except (LibraryError, ConversionError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.get("/kindle")
    def kindle():
        query = request.args.get("q", "").strip()
        status = request.args.get("status", "").strip()
        try:
            books = library.list(query, status)
        except LibraryError:
            status = ""
            books = library.list(query)
        return render_template(
            "kindle.html",
            books=books,
            query=query,
            selected_status=status,
            stats=library.statistics(),
        )

    @app.post("/reading/<book_id>")
    def update_reading(book_id: str):
        destination = request.form.get("return_to", "index")
        endpoint = "kindle" if destination == "kindle" else "index"
        try:
            book = library.update_reading(
                book_id,
                status=request.form.get("status") or None,
                progress=request.form.get("progress"),
                note=request.form.get("note", ""),
            )
            flash(
                f"《{book['title']}》已更新为{STATUS_LABELS[book['status']]}，进度 {book['progress']}%。",
                "success",
            )
        except LibraryError as exc:
            flash(str(exc), "error")
        return redirect(url_for(endpoint, _anchor=f"book-{book_id}"))

    @app.get("/download/<book_id>/<filename>")
    def download(book_id: str, filename: str):
        path = library.file_for(book_id, filename)
        if not path:
            abort(404)
        response = send_file(
            path,
            as_attachment=True,
            download_name=filename,
            mimetype="application/octet-stream",
            conditional=True,
        )
        if request.method == "GET" and response.status_code == 200:
            library.record_download(book_id)
        return response

    @app.post("/remove/<book_id>")
    def remove(book_id: str):
        if library.remove(book_id):
            flash("已从书架移除；文件保留在 data/.trash 中。", "success")
        else:
            flash("没有找到这本书。", "error")
        return redirect(url_for("index"))

    @app.get("/help")
    def help_page():
        return render_template("help.html")

    @app.get("/stats")
    def stats_page():
        before_id = history_cursor()
        events = library.history(100, before_id=before_id)
        return render_template(
            "stats.html",
            stats=library.statistics(),
            history=events,
            next_cursor=events[-1]["id"] if len(events) == 100 else None,
        )

    @app.get("/api/stats")
    def stats_api():
        return library.statistics()

    @app.get("/api/history")
    def history_api():
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))
        events = library.history(
            limit,
            request.args.get("book_id", ""),
            before_id=history_cursor(),
        )
        return {
            "events": events,
            "next_cursor": events[-1]["id"] if len(events) == limit else None,
        }

    def history_cursor() -> int | None:
        value = request.args.get("before_id")
        if value is None:
            return None
        try:
            number = int(value)
            if not 1 <= number <= 2**63 - 1:
                raise ValueError
        except ValueError:
            abort(400, description="历史游标必须是有效范围内的正整数。")
        return number

    @app.get("/export/history")
    def export_history():
        @stream_with_context
        def generate():
            yield '{"events":['
            first = True
            for event in library.iter_history(request.args.get("book_id", "")):
                if not first:
                    yield ","
                yield json.dumps(event, ensure_ascii=False)
                first = False
            yield "]}"

        return Response(
            generate(),
            mimetype="application/json",
            headers={"Content-Disposition": 'attachment; filename="reading-history.json"'},
        )

    @app.get("/api/books")
    def books_api():
        return {"books": library.list()}

    @app.get("/backup/database")
    def database_backup():
        temporary = library.temp_dir / f"backup-{uuid.uuid4().hex}.db"
        try:
            library.backup_to(temporary)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        response = send_file(
            temporary,
            as_attachment=True,
            download_name=f"kindle-shelf-{stamp}.db",
            mimetype="application/vnd.sqlite3",
            conditional=False,
        )
        response.direct_passthrough = False
        response.call_on_close(lambda: temporary.unlink(missing_ok=True))
        return response

    @app.get("/health")
    def health():
        try:
            count = len(library.list())
        except (sqlite3.Error, LibraryError, OSError):
            return {
                "ok": False,
                "calibre": converter.available,
                "database": False,
                "version": __version__,
            }, 503
        ready = converter.available
        return {
            "ok": ready,
            "calibre": converter.available,
            "database": True,
            "books": count,
            "version": __version__,
        }, 200 if ready else 503

    @app.errorhandler(413)
    def too_large(_error):
        flash("文件超过 200 MB，无法上传。", "error")
        return redirect(url_for("index"))

    return app
