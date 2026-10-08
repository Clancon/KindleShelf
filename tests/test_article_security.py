import gzip
import socket
import ssl
from unittest.mock import Mock

import pytest
import urllib3
from bs4 import BeautifulSoup

from kindle_shelf.articles import Article, ArticleError, ArticleExtractor

PUBLIC_IP = "93.184.216.34"
PAGE = b"<html><title>A title</title><article><p>A readable public article.</p></article></html>"
PNG = b"\x89PNG\r\n\x1a\n" + b"image bytes"


class FakeResponse:
    def __init__(self, body=PAGE, *, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "text/html"}
        self.chunks = iter(chunks if chunks is not None else [body, b""])
        self.closed = False
        self.released = False

    def read1(self, size, *, decode_content):
        assert size == 64 * 1024
        assert decode_content is False
        return next(self.chunks, b"")

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


@pytest.fixture
def public_dns(monkeypatch):
    resolver = Mock(
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, 80))]
    )
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return resolver


def fake_network(monkeypatch, *responses):
    pools = []
    queue = iter(responses)

    def build_pool(host, port, **settings):
        pool = Mock()
        pool.host = host
        pool.port = port
        pool.settings = settings
        pool.urlopen.return_value = next(queue)
        pools.append(pool)
        return pool

    constructor = Mock(side_effect=build_pool)
    monkeypatch.setattr(urllib3, "HTTPConnectionPool", constructor)
    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", constructor)
    return constructor, pools


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.8/",
        "http://172.16.0.8/",
        "http://192.168.1.8/",
        "http://169.254.169.254/",
        "http://100.100.100.200/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "http://192.0.0.8/",
        "http://192.88.99.1/",
        "http://168.63.129.16/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://[3fff::1]/",
    ],
)
def test_private_or_special_addresses_never_open_connection(monkeypatch, url):
    constructor, _ = fake_network(monkeypatch)
    with pytest.raises(ArticleError):
        ArticleExtractor().fetch(url)
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "data:text/html,hello",
        "https://user:password@example.com/",
        "https://example.com:22/",
        "https://example.com:100000/",
        "https://example.com/\r\nX-Header:secret",
        "https://example.com\\@127.0.0.1/",
        "http://[fe80::1%25eth0]/",
    ],
)
def test_invalid_schemes_credentials_and_ports_are_rejected(monkeypatch, public_dns, url):
    constructor, _ = fake_network(monkeypatch)
    with pytest.raises(ArticleError) as error:
        ArticleExtractor().fetch(url)
    assert "password" not in str(error.value)
    assert "secret" not in str(error.value)
    constructor.assert_not_called()


def test_dns_with_public_and_private_answer_is_rejected(monkeypatch, public_dns):
    public_dns.return_value.append(
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))
    )
    constructor, _ = fake_network(monkeypatch)
    with pytest.raises(ArticleError):
        ArticleExtractor().fetch("https://example.com/a")
    constructor.assert_not_called()


def test_public_https_is_pinned_and_hostname_verified(monkeypatch, public_dns):
    response = FakeResponse()
    constructor, pools = fake_network(monkeypatch, response)
    article = ArticleExtractor().fetch("https://example.com/中文?id=1")
    assert article.title == "A title"
    public_dns.assert_called_once_with("example.com", 443, type=socket.SOCK_STREAM)
    constructor.assert_called_once()
    pool = pools[0]
    assert pool.host == PUBLIC_IP
    assert pool.settings["server_hostname"] == "example.com"
    assert pool.settings["assert_hostname"] == "example.com"
    assert pool.settings["cert_reqs"] == ssl.CERT_REQUIRED
    args, kwargs = pool.urlopen.call_args
    assert args == ("GET", "/%E4%B8%AD%E6%96%87?id=1")
    assert kwargs["headers"]["Host"] == "example.com"
    assert kwargs["redirect"] is False
    assert kwargs["retries"] is False
    assert response.closed and response.released
    pool.close.assert_called_once()


def test_private_redirect_is_revalidated_and_response_closed(monkeypatch, public_dns):
    redirect = FakeResponse(status=302, headers={"Location": "http://127.0.0.1/admin"})
    constructor, _ = fake_network(monkeypatch, redirect)
    public_dns.side_effect = [
        public_dns.return_value,
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))],
    ]
    with pytest.raises(ArticleError):
        ArticleExtractor().fetch("https://example.com/redirect")
    assert constructor.call_count == 1
    assert redirect.closed and redirect.released


def test_public_redirect_does_not_forward_cookies_or_credentials(monkeypatch, public_dns):
    redirect = FakeResponse(
        status=302,
        headers={
            "Location": "https://another.example.com/article",
            "Set-Cookie": "secret-cookie=private",
        },
    )
    response = FakeResponse()
    _, pools = fake_network(monkeypatch, redirect, response)
    article = ArticleExtractor().fetch("https://example.com/source")
    assert article.url == "https://another.example.com/article"
    assert public_dns.call_count == 2
    assert all(response.closed for response in [redirect, response])
    for pool in pools:
        headers = pool.urlopen.call_args.kwargs["headers"]
        assert "Cookie" not in headers and "Authorization" not in headers


def test_redirect_limit_closes_every_response(monkeypatch, public_dns):
    redirects = [
        FakeResponse(status=302, headers={"Location": "/next"})
        for _ in range(ArticleExtractor.MAX_REDIRECTS + 1)
    ]
    constructor, _ = fake_network(monkeypatch, *redirects)
    with pytest.raises(ArticleError):
        ArticleExtractor().fetch("https://example.com/a")
    assert constructor.call_count == ArticleExtractor.MAX_REDIRECTS + 1
    assert all(response.closed for response in redirects)


@pytest.mark.parametrize("compressed", [False, True])
def test_response_size_limits_decoded_content_and_closes_response(
    monkeypatch, public_dns, compressed
):
    payload = b"a" * 2000
    headers = {"Content-Type": "text/html"}
    if compressed:
        payload = gzip.compress(payload)
        headers["Content-Encoding"] = "gzip"
    response = FakeResponse(payload, headers=headers)
    _, pools = fake_network(monkeypatch, response)
    extractor = ArticleExtractor()
    extractor.MAX_PAGE_BYTES = 100
    with pytest.raises(ArticleError):
        extractor.fetch("https://example.com/bomb")
    assert response.closed and response.released
    pools[0].close.assert_called_once()


def test_valid_gzip_html_is_decoded(monkeypatch, public_dns):
    response = FakeResponse(
        gzip.compress(PAGE),
        headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
    )
    fake_network(monkeypatch, response)
    assert ArticleExtractor().fetch("https://example.com/article").title == "A title"


def test_lowercase_headers_and_declared_chinese_charset(monkeypatch, public_dns):
    response = FakeResponse(
        "<title>中文标题</title><article><p>正确识别中文编码的文章正文。</p></article>".encode(
            "gbk"
        ),
        headers={"content-type": "text/html; charset=gbk"},
    )
    fake_network(monkeypatch, response)
    assert ArticleExtractor().fetch("https://example.com/article").title == "中文标题"


def test_network_error_hides_url_tokens_and_addresses(monkeypatch, public_dns):
    failing_pool = Mock()
    failing_pool.urlopen.side_effect = urllib3.exceptions.HTTPError(
        "cannot fetch https://example.com/?token=private-secret via 192.168.8.1"
    )
    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", Mock(return_value=failing_pool))
    with pytest.raises(ArticleError) as error:
        ArticleExtractor().fetch("https://example.com/?token=private-secret")
    assert "private-secret" not in str(error.value)
    assert "192.168.8.1" not in str(error.value)
    failing_pool.close.assert_called_once()


def test_expired_deadline_does_not_make_request(monkeypatch, public_dns):
    constructor, _ = fake_network(monkeypatch)
    extractor = ArticleExtractor()
    extractor.MAX_FETCH_SECONDS = 0
    with pytest.raises(ArticleError):
        extractor.fetch("https://example.com/article")
    constructor.assert_not_called()


def test_cleaning_removes_root_attrs_active_resources_and_unsafe_schemes():
    article = ArticleExtractor.parse(
        """<article onclick="secret()" style="background:url(file:///private)"
          data-secret="private"><p>Enough readable article content.</p>
          <a href="javascript:alert(1)">unsafe</a>
          <a href="file:///etc/passwd">local</a>
          <a href="data:text/html,bad">data</a>
          <a href="https://user:secret@example.com/">credentials</a>
          <a href="/next">safe</a><a href="#note">fragment</a>
          <link rel="stylesheet" href="http://127.0.0.1/secret">
          <object data="file:///private">private</object>
          <svg><image href="http://127.0.0.1/a"></image></svg>
          <img src="file:///private" onerror="bad()">
          <img data-src="/image.png" srcset="http://127.0.0.1/secret">
          <custom-element data-key="value">keep this text</custom-element>
          </article>""",
        "https://example.com/read",
    )
    soup = BeautifulSoup(article.content_html, "html.parser")
    assert not soup.article.attrs
    assert not soup.find(["script", "link", "object", "svg", "custom-element"])
    assert "keep this text" in soup.get_text()
    assert [link.get("href") for link in soup.find_all("a")] == [
        None,
        None,
        None,
        None,
        "https://example.com/next",
        "#note",
    ]
    assert soup.find_all("img")[0].attrs == {}
    assert soup.find_all("img")[1]["src"] == "https://example.com/image.png"
    assert "secret" not in article.content_html and "onerror" not in article.content_html


def test_selected_script_cannot_be_article_root():
    with pytest.raises(ArticleError):
        ArticleExtractor.parse(
            '<script id="js_content">some malicious javascript content()</script>',
            "https://example.com/",
        )


@pytest.mark.parametrize(
    "document",
    [
        "<html><title>环境异常</title><div>" + "请完成验证，继续访问。" * 30 + "</div></html>",
        "<html><div class='weui-msg'>" + "该内容已被发布者删除。" * 30 + "</div></html>",
    ],
)
def test_wechat_verification_or_deleted_page_is_not_imported(document):
    with pytest.raises(ArticleError, match="微信"):
        ArticleExtractor.parse(document, "https://mp.weixin.qq.com/s/123")


def test_wechat_article_can_discuss_validation_without_being_rejected():
    article = ArticleExtractor.parse(
        "<html><title>环境异常排查方法</title><div id='js_content'>"
        "<p>本文讨论环境异常和完成验证的操作，请完整阅读正文。</p></div></html>",
        "https://mp.weixin.qq.com/s/123",
    )
    assert article.title == "环境异常排查方法"


def test_private_images_are_removed_without_connection(monkeypatch, tmp_path):
    constructor, _ = fake_network(monkeypatch)
    article = Article(
        "https://example.com/article",
        "Title",
        "",
        '<p>Readable article.</p><img src="http://127.0.0.1/private">',
    )
    output = ArticleExtractor().build_book_html(article, tmp_path)
    assert not BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser").find("img")
    constructor.assert_not_called()


def test_valid_image_is_localized_and_referer_drops_secret_query(monkeypatch, public_dns, tmp_path):
    response = FakeResponse(PNG, headers={"Content-Type": "image/png"})
    _, pools = fake_network(monkeypatch, response)
    article = Article(
        "https://example.com/article?token=secret",
        "Title",
        "",
        '<p>Readable article.</p><img src="https://image.example.com/image">',
    )
    output = ArticleExtractor().build_book_html(article, tmp_path)
    soup = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
    assert soup.img["src"] == "images/image-01.png"
    assert (tmp_path / "images/image-01.png").read_bytes() == PNG
    assert pools[0].urlopen.call_args.kwargs["headers"]["Referer"] == "https://example.com/"
    assert response.closed


def test_disguised_html_or_svg_image_is_not_saved(monkeypatch, public_dns, tmp_path):
    response = FakeResponse(
        b"<svg><script>bad()</script></svg>", headers={"Content-Type": "image/jpeg"}
    )
    fake_network(monkeypatch, response)
    soup = BeautifulSoup('<img src="https://example.com/picture.jpg">', "html.parser")
    ArticleExtractor()._download_images(soup, "https://example.com/", tmp_path)
    assert not soup.find("img")
    assert not (tmp_path / "images").exists()


def test_failed_images_count_towards_attempt_limit(monkeypatch, public_dns, tmp_path):
    extractor = ArticleExtractor()
    extractor.MAX_IMAGES = 3
    failures = [FakeResponse(status=404) for _ in range(3)]
    constructor, _ = fake_network(monkeypatch, *failures)
    soup = BeautifulSoup(
        "".join(f'<img src="https://example.com/{number}.png">' for number in range(50)),
        "html.parser",
    )
    extractor._download_images(soup, "https://example.com/", tmp_path)
    assert constructor.call_count == 3
    assert not soup.find("img")
    assert all(response.closed for response in failures)


def test_image_total_bytes_budget_is_enforced(monkeypatch, public_dns, tmp_path):
    extractor = ArticleExtractor()
    extractor.MAX_TOTAL_IMAGE_BYTES = len(PNG)
    constructor, _ = fake_network(
        monkeypatch, FakeResponse(PNG, headers={"Content-Type": "image/png"})
    )
    soup = BeautifulSoup(
        '<img src="https://example.com/one"><img src="https://example.com/two">',
        "html.parser",
    )
    extractor._download_images(soup, "https://example.com/", tmp_path)
    assert constructor.call_count == 1
    assert len(soup.find_all("img")) == 1


def test_pasted_text_escapes_html_and_preserves_paragraphs(tmp_path):
    output = ArticleExtractor().build_text_html(
        "<title>", "<author>", 'first <script>alert("x")</script>\nline\n\nsecond', tmp_path
    )
    soup = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
    assert soup.title.get_text() == "<title>"
    assert not soup.find("script")
    assert len(soup.find_all("p")) == 3
    assert soup.find("br") is not None
