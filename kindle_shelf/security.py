from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from flask import abort, request, session


def session_secret(data_dir: Path, configured: str | None) -> str:
    if configured:
        return configured
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_path = data_dir / ".session-secret"
    try:
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = secret_path.read_text(encoding="ascii").strip()
        if len(value) < 32:
            raise RuntimeError("本地会话密钥无效，请恢复数据目录中的 .session-secret。")
        return value
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        value = secrets.token_urlsafe(48)
        output.write(value)
    return value


def public_base_url(value: str) -> str:
    if not value:
        return ""
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("KINDLE_SHELF_PUBLIC_URL 必须是无账号、无路径的 http(s) 服务地址。")
    return value


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def protect_form(public_url: str) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    # Origin is supplementary; older Kindle browsers may omit it.
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") not in {
        request.host_url.rstrip("/"),
        public_url,
    }:
        abort(400, description="请求来源不匹配，请重新打开本服务页面。")
    expected = session.get("csrf_token")
    supplied = request.form.get("csrf_token", "")
    if (
        not isinstance(expected, str)
        or not supplied
        or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
    ):
        abort(400, description="表单已失效，请刷新页面后重新提交。")
