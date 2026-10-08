from __future__ import annotations

import html
import ipaddress
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import certifi
import urllib3
from bs4 import BeautifulSoup, Comment, Tag, UnicodeDammit


class ArticleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    author: str
    content_html: str


@dataclass(frozen=True)
class _PublicUrl:
    url: str
    scheme: str
    hostname: str
    port: int
    address: str
    target: str
    host_header: str


class ArticleExtractor:
    MAX_PAGE_BYTES = 20 * 1024 * 1024
    MAX_IMAGES = 30
    MAX_IMAGE_BYTES = 8 * 1024 * 1024
    MAX_TOTAL_IMAGE_BYTES = 30 * 1024 * 1024
    MAX_REDIRECTS = 5
    MAX_FETCH_SECONDS = 60
    MAX_IMAGE_SECONDS = 45
    # Keep the policy consistent on Python 3.10/3.11, whose ipaddress tables
    # predate newer special-purpose allocations. Transition tunnels can also
    # route apparently public IPv6 addresses to private IPv4 endpoints.
    BLOCKED_NETWORKS = tuple(
        ipaddress.ip_network(value)
        for value in (
            "0.0.0.0/8",
            "10.0.0.0/8",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.0.0.0/24",
            "192.0.2.0/24",
            "192.88.99.0/24",
            "192.168.0.0/16",
            "198.18.0.0/15",
            "198.51.100.0/24",
            "203.0.113.0/24",
            "224.0.0.0/4",
            "240.0.0.0/4",
            "168.63.129.16/32",
            "2001::/23",
            "2002::/16",
            "3fff::/20",
        )
    )
    PUBLIC_IPV6_NETWORK = ipaddress.ip_network("2000::/3")
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36 KindleShelf/1.0"
    )
    CONTENT_TAGS = {
        "html",
        "body",
        "article",
        "main",
        "section",
        "div",
        "span",
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "hr",
        "a",
        "img",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "del",
        "ins",
        "sub",
        "sup",
        "small",
        "mark",
        "blockquote",
        "pre",
        "code",
        "kbd",
        "samp",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "caption",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "figure",
        "figcaption",
    }
    DROP_TAGS = {
        "head",
        "script",
        "style",
        "noscript",
        "svg",
        "math",
        "form",
        "input",
        "button",
        "textarea",
        "select",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "canvas",
        "video",
        "audio",
        "source",
        "link",
        "meta",
        "base",
        "nav",
        "footer",
    }

    def __init__(self, timeout: int = 25) -> None:
        self.timeout = max(1, min(timeout, 25))

    @staticmethod
    def _public_address(value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        if not address.is_global or address.is_multicast or address.is_reserved:
            return False
        if any(address in network for network in ArticleExtractor.BLOCKED_NETWORKS):
            return False
        if isinstance(address, ipaddress.IPv6Address) and (
            address not in ArticleExtractor.PUBLIC_IPV6_NETWORK
        ):
            return False
        return True

    @classmethod
    def _resolve_public_url(cls, value: str) -> _PublicUrl:
        value = value.strip()
        if re.search(r"[\x00-\x20\x7f\\]", value):
            raise ArticleError("网页地址包含无效字符。")
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("scheme or hostname")
            if parsed.username is not None or parsed.password is not None:
                raise ArticleError("不支持在网页地址中提供登录凭据。")
            hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not hostname or "%" in hostname or port not in {80, 443}:
                raise ArticleError("只支持使用 80 或 443 端口的公开网页。")
            resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except ArticleError:
            raise
        except (ValueError, UnicodeError, OSError) as exc:
            raise ArticleError("网页地址无效或暂时无法解析。") from exc
        addresses = list(dict.fromkeys(item[4][0] for item in resolved))
        if not addresses or not all(cls._public_address(item) for item in addresses):
            raise ArticleError("只允许抓取公开互联网网页，不能访问本机或局域网地址。")

        host_header = f"[{hostname}]" if ":" in hostname else hostname
        if port != (443 if parsed.scheme == "https" else 80):
            host_header += f":{port}"
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~")
        target = path + (f"?{query}" if query else "")
        normalized = urlunsplit((parsed.scheme, host_header, path, query, ""))
        return _PublicUrl(
            normalized, parsed.scheme, hostname, port, addresses[0], target, host_header
        )

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ArticleError("读取网页超时，请稍后重试或改用粘贴正文。")
        return remaining

    def _read_public_url(
        self,
        url: str,
        *,
        max_bytes: int,
        deadline: float,
        referer: str = "",
    ) -> tuple[bytes, str, dict]:
        current_url = url
        for redirect_number in range(self.MAX_REDIRECTS + 1):
            self._remaining(deadline)
            destination = self._resolve_public_url(current_url)
            headers = {
                "User-Agent": self.USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Accept-Encoding": "gzip",
                "Host": destination.host_header,
            }
            if referer:
                source = urlsplit(self._safe_href(referer, ""))
                if source.scheme in {"http", "https"} and source.hostname:
                    headers["Referer"] = urlunsplit((source.scheme, source.netloc, "/", "", ""))
            timeout = urllib3.Timeout(
                connect=min(self.timeout, self._remaining(deadline)),
                read=min(self.timeout, self._remaining(deadline)),
            )
            # Pin the socket to a validated IP; HTTPS still verifies the source
            # hostname. Resolving a second time would allow DNS rebinding.
            if destination.scheme == "https":
                pool = urllib3.HTTPSConnectionPool(
                    destination.address,
                    destination.port,
                    server_hostname=destination.hostname,
                    assert_hostname=destination.hostname,
                    cert_reqs=ssl.CERT_REQUIRED,
                    ca_certs=certifi.where(),
                )
            else:
                pool = urllib3.HTTPConnectionPool(destination.address, destination.port)
            response = None
            try:
                response = pool.urlopen(
                    "GET",
                    destination.target,
                    headers=headers,
                    timeout=timeout,
                    retries=False,
                    redirect=False,
                    preload_content=False,
                    decode_content=False,
                )
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status in {301, 302, 303, 307, 308}:
                    location = response_headers.get("location")
                    if not location or redirect_number >= self.MAX_REDIRECTS:
                        raise ArticleError("网页重定向次数过多或地址无效。")
                    current_url = urljoin(destination.url, location)
                    continue
                if response.status >= 400:
                    raise ArticleError("网页暂时无法读取，可能需要登录或验证。")
                if response.status < 200 or response.status >= 300:
                    raise ArticleError("网页返回了无法处理的响应。")

                content_encoding = response_headers.get("content-encoding", "").lower().strip()
                if content_encoding not in {"", "identity", "gzip", "x-gzip"}:
                    raise ArticleError("网页使用了暂不支持的压缩格式。")
                decoder = (
                    zlib.decompressobj(16 + zlib.MAX_WBITS)
                    if content_encoding in {"gzip", "x-gzip"}
                    else None
                )
                chunks: list[bytes] = []
                total = raw_total = 0
                while True:
                    remaining = min(self.timeout, self._remaining(deadline))
                    connection = getattr(response, "connection", None)
                    connected_socket = getattr(connection, "sock", None)
                    if connected_socket is not None:
                        connected_socket.settimeout(remaining)
                    chunk = response.read1(64 * 1024, decode_content=False)
                    if not chunk:
                        break
                    raw_total += len(chunk)
                    if raw_total > max_bytes:
                        raise ArticleError("网页或图片过大，无法安全处理。")
                    if decoder:
                        chunk = decoder.decompress(chunk, max_bytes - total + 1)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArticleError("网页或图片过大，无法安全处理。")
                    chunks.append(chunk)
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise ArticleError("网页压缩内容无效。")
                return b"".join(chunks), destination.url, response_headers
            except ArticleError:
                raise
            except (urllib3.exceptions.HTTPError, OSError, ValueError, zlib.error) as exc:
                raise ArticleError("无法读取网页，请检查地址或稍后重试。") from exc
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()
                pool.close()
        raise ArticleError("网页重定向次数过多。")

    def fetch(self, url: str) -> Article:
        raw, final_url, headers = self._read_public_url(
            url,
            max_bytes=self.MAX_PAGE_BYTES,
            deadline=time.monotonic() + self.MAX_FETCH_SECONDS,
        )
        content_type = Message()
        content_type["content-type"] = headers.get("content-type", "")
        if headers.get("content-type") and content_type.get_content_type() not in {
            "text/html",
            "application/xhtml+xml",
            "text/plain",
        }:
            raise ArticleError("该地址没有返回文章网页，请上传电子书文件。")
        charset = content_type.get_param("charset")
        decoded = UnicodeDammit(
            raw,
            known_definite_encodings=[charset] if charset else [],
            is_html=True,
        ).unicode_markup
        if decoded is None:
            raise ArticleError("无法识别网页文字编码，可以改用粘贴正文。")
        return self.parse(decoded, final_url)

    @staticmethod
    def _meta(soup: BeautifulSoup, *names: str) -> str:
        for name in names:
            node = soup.find("meta", attrs={"property": name}) or soup.find(
                "meta", attrs={"name": name}
            )
            if node and node.get("content"):
                return str(node["content"]).strip()
        return ""

    @classmethod
    def parse(cls, document: str, url: str) -> Article:
        soup = BeautifulSoup(document, "html.parser")
        title = cls._meta(soup, "og:title", "twitter:title")
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        if not title:
            heading = soup.find("h1")
            title = heading.get_text(" ", strip=True) if heading else "未命名文章"

        author = cls._meta(soup, "author", "article:author", "og:article:author")
        if not author:
            author_node = soup.select_one("#js_name, .author, [rel=author]")
            if author_node:
                author = author_node.get_text(" ", strip=True)

        root = soup.select_one("#js_content")
        if (urlsplit(url).hostname or "").lower() == "mp.weixin.qq.com" and (
            root is None or len(root.get_text(" ", strip=True)) < 8
        ):
            validation_text = soup.get_text(" ", strip=True)
            markers = (
                "环境异常",
                "完成验证",
                "访问过于频繁",
                "需要验证",
                "该内容已被发布者删除",
                "此内容因违规无法查看",
                "该内容已被投诉",
                "链接已过期",
                "账号已迁移",
            )
            if any(marker in validation_text for marker in markers) or soup.select_one(
                "#verify, #captcha, .weui-msg"
            ):
                raise ArticleError("微信返回了验证或失效页面，请在浏览器中查看后粘贴正文。")
        if root is None:
            root = soup.find("article") or soup.find("main") or soup.select_one("[role=main]")
        if root is None:
            root = cls._best_content_node(soup)
        if root is None:
            raise ArticleError("没有从网页中识别出可阅读的正文。可以改用“粘贴正文”。")

        cls._clean_content(root, url)
        if len(root.get_text(" ", strip=True)) < 8:
            raise ArticleError("没有从网页中识别出可阅读的正文。可以改用“粘贴正文”。")
        clean_title = re.sub(r"\s+", " ", title).strip()[:240]
        clean_author = re.sub(r"\s+", " ", author).strip()[:120]
        return Article(url=url, title=clean_title, author=clean_author, content_html=str(root))

    @staticmethod
    def _best_content_node(soup: BeautifulSoup) -> Tag | None:
        candidates = soup.find_all(["div", "section", "body"])
        best: Tag | None = None
        best_score = 0.0
        for node in candidates:
            text_length = len(node.get_text(" ", strip=True))
            if text_length < 150:
                continue
            link_length = sum(len(link.get_text(" ", strip=True)) for link in node.find_all("a"))
            paragraph_bonus = len(node.find_all("p")) * 80
            score = text_length - link_length * 1.8 + paragraph_bonus
            if score > best_score:
                best, best_score = node, score
        return best

    @staticmethod
    def _safe_href(value: str, base_url: str) -> str:
        if value.startswith("#"):
            return value
        try:
            candidate = urljoin(base_url, value)
            parsed = urlsplit(candidate)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or re.search(r"[\x00-\x20\x7f\\]", candidate)
            ):
                return ""
            return candidate
        except ValueError:
            return ""

    @classmethod
    def _clean_content(cls, root: Tag, base_url: str) -> None:
        if root.name in cls.DROP_TAGS:
            root.clear()
            root.name = "div"
            root.attrs = {}
            return
        if root.name not in cls.CONTENT_TAGS and root.name != "[document]":
            root.name = "div"
        for comment in root.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()
        for node in list(root.find_all(cls.DROP_TAGS)):
            node.decompose()

        allowed_attrs = {
            "a": {"href"},
            "img": {"src", "data-src", "data-original", "alt", "title"},
            "td": {"colspan", "rowspan"},
            "th": {"colspan", "rowspan"},
        }
        # The selected container is part of the untrusted page too.
        for node in [root, *root.find_all(True)]:
            if node.name not in cls.CONTENT_TAGS and node is not root:
                node.unwrap()
                continue
            keep = allowed_attrs.get(node.name, set())
            node.attrs = {key: value for key, value in node.attrs.items() if key in keep}
            if node.name == "a" and node.get("href"):
                href = cls._safe_href(str(node["href"]).strip(), base_url)
                if href:
                    node["href"] = href
                else:
                    node.attrs.pop("href", None)
            if node.name == "img":
                source = node.get("data-src") or node.get("data-original") or node.get("src")
                href = cls._safe_href(str(source or "").strip(), base_url)
                if href and not href.startswith("#"):
                    node["src"] = href
                else:
                    node.attrs.pop("src", None)
                node.attrs.pop("data-src", None)
                node.attrs.pop("data-original", None)
            for key in {"colspan", "rowspan"} & node.attrs.keys():
                value = str(node[key])
                if not value.isdigit() or not 1 <= int(value) <= 100:
                    node.attrs.pop(key, None)

    def build_book_html(self, article: Article, work_dir: Path) -> Path:
        work_dir.mkdir(parents=True, exist_ok=True)
        soup = BeautifulSoup(article.content_html, "html.parser")
        self._clean_content(soup, article.url)
        self._download_images(soup, article.url, work_dir)
        content = str(soup)
        byline = f'<p class="byline">{html.escape(article.author)}</p>' if article.author else ""
        source = self._safe_href(article.url, "")
        source_markup = (
            f'<p class="source">来源：<a href="{html.escape(source, quote=True)}">'
            f"{html.escape(source)}</a></p>"
            if source
            else ""
        )
        document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(article.title)}</title>
  <style>
    body {{ font-family: serif; line-height: 1.65; margin: 5%; }}
    h1 {{ line-height: 1.25; }}
    img {{ max-width: 100%; height: auto; }}
    .byline, .source {{ color: #555; font-size: 0.9em; }}
    blockquote {{ margin-left: 1em; border-left: 3px solid #888; padding-left: 0.8em; }}
    pre {{ white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h1>{html.escape(article.title)}</h1>
  {byline}
  {source_markup}
  <hr>
  {content}
</body>
</html>"""
        output = work_dir / "article.html"
        output.write_text(document, encoding="utf-8")
        return output

    def build_text_html(self, title: str, author: str, text: str, work_dir: Path) -> Path:
        paragraphs = []
        for block in re.split(r"\n\s*\n", text.strip()):
            escaped = html.escape(block.strip()).replace("\n", "<br>")
            if escaped:
                paragraphs.append(f"<p>{escaped}</p>")
        article = Article(
            url="",
            title=title,
            author=author,
            content_html="\n".join(paragraphs),
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        byline = f'<p class="byline">{html.escape(author)}</p>' if author else ""
        document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(title)}</title><style>body{{font-family:serif;line-height:1.65;margin:5%}}.byline{{color:#555}}</style>
</head><body><h1>{html.escape(title)}</h1>{byline}{article.content_html}</body></html>"""
        output = work_dir / "article.html"
        output.write_text(document, encoding="utf-8")
        return output

    @staticmethod
    def raster_extension(content: bytes) -> str:
        if content.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if content.startswith(b"BM"):
            return ".bmp"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return ".webp"
        return ""

    def _download_images(self, soup: BeautifulSoup, referer: str, work_dir: Path) -> None:
        image_dir = work_dir / "images"
        downloaded = attempted = total = 0
        deadline = time.monotonic() + self.MAX_IMAGE_SECONDS
        for image in soup.find_all("img"):
            source = str(image.get("src") or "")
            if (
                not source
                or attempted >= self.MAX_IMAGES
                or total >= self.MAX_TOTAL_IMAGE_BYTES
                or time.monotonic() >= deadline
            ):
                image.decompose()
                continue
            attempted += 1
            try:
                content, _, _ = self._read_public_url(
                    source,
                    max_bytes=min(self.MAX_IMAGE_BYTES, self.MAX_TOTAL_IMAGE_BYTES - total),
                    deadline=deadline,
                    referer=referer,
                )
                total += len(content)
                extension = self.raster_extension(content)
                if not extension:
                    raise ValueError("not a raster image")
                image_dir.mkdir(exist_ok=True)
                filename = f"image-{downloaded + 1:02d}{extension}"
                (image_dir / filename).write_bytes(content)
                image["src"] = f"images/{filename}"
                downloaded += 1
            except (ArticleError, OSError, ValueError):
                image.decompose()
