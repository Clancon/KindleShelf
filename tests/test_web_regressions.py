import sqlite3

import pytest
from bs4 import BeautifulSoup

from kindle_shelf.web import create_app


class FakeConverter:
    available = True

    def convert(self, source, destination, **_kwargs):
        destination.write_bytes(b"BOOKMOBI" + source.read_bytes())


@pytest.fixture
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path,
            "CONVERTER": FakeConverter(),
        }
    )


def form_token(client, path="/"):
    html = BeautifulSoup(client.get(path).get_data(as_text=True), "html.parser")
    return html.find("input", attrs={"name": "csrf_token"})["value"]


@pytest.mark.parametrize(
    "path",
    ["/add/text", "/add/url", "/add/upload", "/reading/example", "/remove/example"],
)
def test_post_endpoints_reject_missing_csrf(app, path):
    response = app.test_client().post(path, data={"title": "test", "text": "test text"})
    assert response.status_code == 400
    assert app.extensions["shelf_library"].list() == []


def test_cross_origin_form_is_rejected_even_with_valid_token(app):
    client = app.test_client()
    token = form_token(client)
    response = client.post(
        "/add/text",
        data={"csrf_token": token, "title": "test", "text": "test text"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 400
    assert app.extensions["shelf_library"].list() == []


def test_invalid_non_ascii_token_returns_400_not_500(app):
    client = app.test_client()
    form_token(client)
    assert client.post("/add/text", data={"csrf_token": "无效"}).status_code == 400


def test_csrf_tokens_are_bound_to_session(app):
    token = form_token(app.test_client())
    assert app.test_client().post("/add/text", data={"csrf_token": token}).status_code == 400


def test_every_rendered_post_form_has_token_on_pc_and_kindle(app):
    library = app.extensions["shelf_library"]
    library.add_text("a book", "", "some text")
    client = app.test_client()
    for path in ("/", "/kindle"):
        page = BeautifulSoup(client.get(path).get_data(as_text=True), "html.parser")
        forms = page.find_all("form", attrs={"method": "post"})
        assert forms
        for form in forms:
            assert form.find("input", attrs={"name": "csrf_token"})["value"]


def test_legacy_kindle_form_can_submit_without_origin(app, post_form):
    library = app.extensions["shelf_library"]
    book = library.add_text("a book", "", "some text")
    client = app.test_client()
    token = form_token(client, "/kindle")
    response = client.post(
        f"/reading/{book['id']}",
        data={
            "csrf_token": token,
            "status": "finished",
            "return_to": "kindle",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert library.get(book["id"])["status"] == "finished"


def test_secret_is_random_persistent_and_excluded_from_database(app, tmp_path):
    secret = app.config["SECRET_KEY"]
    assert len(secret) >= 32
    assert (tmp_path / ".session-secret").read_text(encoding="ascii") == secret
    second = create_app({"TESTING": True, "DATA_DIR": tmp_path, "CONVERTER": FakeConverter()})
    assert second.config["SECRET_KEY"] == secret
    assert secret.encode() not in (tmp_path / "kindle_shelf.db").read_bytes()


def test_untrusted_host_rejected(app):
    assert app.test_client().get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_server_public_url_overrides_container_private_address(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path,
            "CONVERTER": FakeConverter(),
            "PUBLIC_URL": "http://shelf.example:8091",
        }
    )
    response = app.test_client().get("/", base_url="http://shelf.example:8091")
    assert response.status_code == 200
    assert "http://shelf.example:8091/kindle" in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "public_url",
    ["ftp://example.com", "http://u:p@example.com", "http://example.com/path", "http://[oops"],
)
def test_invalid_public_url_configuration_fails_early(tmp_path, public_url):
    with pytest.raises(ValueError):
        create_app({"DATA_DIR": tmp_path, "PUBLIC_URL": public_url})


def test_failed_conditional_downloads_do_not_inflate_history(app):
    library = app.extensions["shelf_library"]
    book = library.add_text("a book", "", "some text")
    client = app.test_client()
    url = f"/download/{book['id']}/{book['filename']}"
    head = client.head(url)
    response = client.get(url, headers={"If-None-Match": head.headers["ETag"]})
    assert response.status_code == 304
    assert library.get(book["id"])["download_count"] == 0
    assert client.get(url, headers={"Range": "bytes=999999999-"}).status_code == 416
    assert library.get(book["id"])["download_count"] == 0
    assert client.get(url).status_code == 200
    assert library.get(book["id"])["download_count"] == 1


def test_missing_converter_health_is_unready(tmp_path):
    class MissingConverter:
        available = False

    app = create_app({"TESTING": True, "DATA_DIR": tmp_path, "CONVERTER": MissingConverter()})
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.get_json()["ok"] is False
    assert response.get_json()["database"] is True


def test_corrupt_database_health_is_unready(app, tmp_path):
    with sqlite3.connect(tmp_path / "kindle_shelf.db") as connection:
        connection.execute("DROP TABLE books")
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.get_json()["database"] is False


def test_backup_file_cleaned_when_response_closes(app, tmp_path):
    client = app.test_client()
    response = client.get("/backup/database")
    assert response.data.startswith(b"SQLite format 3\x00")
    response.close()
    assert not list((tmp_path / ".working").glob("backup-*.db"))


def test_history_pages_and_full_export_exceed_old_500_limit(app):
    library = app.extensions["shelf_library"]
    book = library.add_text("a book", "", "some text")
    for _index in range(501):
        library.record_download(book["id"])
    client = app.test_client()
    first = client.get("/api/history?limit=100").get_json()
    second = client.get(f"/api/history?limit=100&before_id={first['next_cursor']}").get_json()
    assert len(first["events"]) == len(second["events"]) == 100
    assert {item["id"] for item in first["events"]}.isdisjoint(
        {item["id"] for item in second["events"]}
    )
    full = client.get("/export/history").get_json()
    assert len(full["events"]) == 502
    assert client.get("/stats?before_id=not-a-number").status_code == 400


def test_private_content_is_not_browser_cached(app):
    client = app.test_client()
    for path in ("/", "/stats", "/api/books", "/api/history", "/backup/database"):
        response = client.get(path)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Frame-Options"] == "DENY"
        response.close()


@pytest.mark.parametrize("path", ["/api/history", "/stats"])
@pytest.mark.parametrize("cursor", ["0", "-1", str(2**63), "9" * 100])
def test_history_rejects_out_of_range_cursor_without_server_error(app, path, cursor):
    assert app.test_client().get(f"{path}?before_id={cursor}").status_code == 400


@pytest.mark.parametrize("path", ["/api/history", "/stats"])
def test_history_accepts_largest_sqlite_cursor(app, path):
    assert app.test_client().get(f"{path}?before_id={2**63 - 1}").status_code == 200


def test_trusted_host_configuration_trims_whitespace(tmp_path, monkeypatch):
    monkeypatch.setenv("KINDLE_SHELF_TRUSTED_HOSTS", " shelf.example , second.example, ")
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path, "CONVERTER": FakeConverter()})
    assert app.test_client().get("/", base_url="http://shelf.example").status_code == 200
    assert app.test_client().get("/", base_url="http://second.example").status_code == 200
    assert app.test_client().get("/", base_url="http://other.example").status_code == 400
