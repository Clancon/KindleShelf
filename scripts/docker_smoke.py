"""Build and test Linux Docker using only disposable source and data directories."""

from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def docker(*arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
        timeout=900,
    )
    return result.stdout.strip()


def published_url(name: str) -> str:
    addresses = docker("port", name, "8090/tcp").splitlines()
    if not addresses:
        raise AssertionError(f"Container has no published HTTP port: {name}")
    return f"http://{addresses[0]}"


class TokenParser(HTMLParser):
    token = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "csrf_token":
            self.token = attributes.get("value") or ""


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def get(self, path: str) -> bytes:
        with self.opener.open(f"{self.base_url}{path}", timeout=30) as response:
            return response.read()

    def json(self, path: str) -> dict:
        return json.loads(self.get(path))

    def post(self, path: str, fields: dict[str, str]) -> None:
        parser = TokenParser()
        parser.feed(self.get("/").decode("utf-8"))
        if not parser.token:
            raise AssertionError("Management forms must provide a CSRF token.")
        payload = urllib.parse.urlencode({**fields, "csrf_token": parser.token}).encode()
        request = urllib.request.Request(f"{self.base_url}{path}", data=payload)
        with self.opener.open(request, timeout=360) as response:
            if response.status != 200:
                raise AssertionError(f"Unexpected form result: {response.status}")


def wait_ready(name: str, base_url: str) -> Client:
    deadline = time.monotonic() + 180
    last_error: urllib.error.URLError | None = None
    while time.monotonic() < deadline:
        state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
        if not state.get("Running"):
            raise AssertionError(f"Container exited: {docker('logs', name)}")
        if state.get("Health", {}).get("Status") == "healthy":
            client = Client(base_url)
            try:
                result = client.json("/health")
            except urllib.error.URLError as exc:
                last_error = exc
            else:
                if not all(result.get(key) is True for key in ("ok", "database", "calibre")):
                    raise AssertionError(f"Incomplete readiness: {result}")
                return client
        time.sleep(1)
    detail = f"; last HTTP error: {last_error}" if last_error else ""
    raise AssertionError(f"Container did not become ready{detail}: {docker('logs', name)}")


def prepare_context(destination: Path) -> None:
    for filename in (".dockerignore", "Dockerfile", "requirements.txt", "app.py"):
        shutil.copy2(ROOT / filename, destination / filename)
    shutil.copytree(
        ROOT / "kindle_shelf",
        destination / "kindle_shelf",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (destination / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "healthcheck.py", destination / "scripts" / "healthcheck.py")
    # Sentinels represent material that must never reach the build context.
    for relative in (
        "data/private-reading.db",
        "logs/private.log",
        ".env",
        ".git/private-config",
        ".venv/private-secret",
        "kindle_shelf/__pycache__/private.pyc",
        "kindle_shelf/private.pem",
    ):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DO-NOT-SEND-PRIVATE-CONTENT", encoding="ascii")


def check_context(context: Path, image: str, name: str) -> None:
    docker(
        "build",
        "--tag",
        image,
        "--file",
        "-",
        str(context),
        input_text="FROM scratch\nCOPY . /context/\n",
    )
    docker("create", "--name", name, image, "/unused")
    result = subprocess.run(["docker", "export", name], capture_output=True, check=True, timeout=60)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        names = set(archive.getnames())
        if "context/app.py" not in names or "context/scripts/healthcheck.py" not in names:
            raise AssertionError("The Docker context is missing runtime source.")
        for member in archive.getmembers():
            if member.isfile():
                payload = archive.extractfile(member).read()
                if b"DO-NOT-SEND-PRIVATE-CONTENT" in payload:
                    raise AssertionError(
                        f"Private content entered the Docker context: {member.name}"
                    )


def exercise(name: str, client: Client) -> None:
    client.post(
        "/add/text",
        {
            "title": "Docker persistence smoke",
            "author": "Automated regression",
            "text": "A complete article used to test a real Calibre conversion.\n\n"
            "The resulting MOBI and reading history must survive a container restart.",
            "target": "mobi",
        },
    )
    books = client.json("/api/books")["books"]
    if len(books) != 1:
        raise AssertionError(f"The real converter did not create one book: {books}")
    book = books[0]
    download_path = f"/download/{book['id']}/{urllib.parse.quote(book['filename'], safe='')}"
    mobi = client.get(download_path)
    if len(mobi) <= 1000 or b"BOOKMOBI" not in mobi[:256]:
        raise AssertionError("The download is not a real MOBI file.")
    expected_hash = hashlib.sha256(mobi).hexdigest()
    client.post(
        f"/reading/{book['id']}",
        {"status": "finished", "note": "Container reading history smoke", "return_to": "index"},
    )
    stats = client.json("/api/stats")
    if stats["finished"] != 1 or stats["downloads"] != 1:
        raise AssertionError(f"Reading or download statistics are incorrect: {stats}")
    history_before = client.json("/api/history")["events"]
    if not any(event["event_type"] == "status_changed" for event in history_before):
        raise AssertionError("Reading history was not recorded.")
    if b"<script" in client.get("/kindle").lower():
        raise AssertionError("The Kindle page must work without JavaScript.")

    docker("stop", "--time", "30", name)
    state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
    if state["Running"] or state["ExitCode"] == 137:
        raise AssertionError(f"Container did not stop without SIGKILL: {state}")
    docker("start", name)
    client = wait_ready(name, published_url(name))
    retained = client.json("/api/books")["books"]
    if len(retained) != 1 or retained[0]["status"] != "finished":
        raise AssertionError("Reading state did not survive restart.")
    if client.json("/api/history")["events"] != history_before:
        raise AssertionError("Reading history did not survive restart.")
    if hashlib.sha256(client.get(download_path)).hexdigest() != expected_hash:
        raise AssertionError("The generated MOBI changed or disappeared after restart.")


def main() -> int:
    if not shutil.which("docker"):
        raise SystemExit("Docker is not installed; no container checks have been performed.")
    docker("info", "--format", "{{json .ServerVersion}}")
    suffix = uuid.uuid4().hex[:12]
    image = f"kindle-shelf-smoke:{suffix}"
    context_image = f"kindle-shelf-context-smoke:{suffix}"
    name = f"kindle-shelf-smoke-{suffix}"
    context_name = f"kindle-shelf-context-{suffix}"
    try:
        with tempfile.TemporaryDirectory(prefix="kindle-shelf-docker-") as temporary:
            directory = Path(temporary)
            context = directory / "source"
            context.mkdir()
            prepare_context(context)
            check_context(context, context_image, context_name)
            docker("build", "--tag", image, str(context))
            settings = json.loads(docker("image", "inspect", "--format", "{{json .Config}}", image))
            if settings["User"] != "10001:10001":
                raise AssertionError("Application must run as the dedicated nonroot user.")
            if settings["Entrypoint"] != ["/usr/bin/tini", "--"]:
                raise AssertionError("Application needs a PID 1 signal/reaping supervisor.")
            data = directory / "data"
            data.mkdir()
            try:
                # Assign only this disposable bind mount; production uses documented ownership.
                docker(
                    "run",
                    "--rm",
                    "--user",
                    "0:0",
                    "--mount",
                    f"type=bind,source={data},target=/app/data",
                    image,
                    "python",
                    "-c",
                    "import os; os.chown('/app/data',10001,10001)",
                )
                docker(
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--restart",
                    "unless-stopped",
                    "--memory",
                    "2g",
                    "--cpus",
                    "2",
                    "--pids-limit",
                    "256",
                    "--publish",
                    "127.0.0.1::8090",
                    "--mount",
                    f"type=bind,source={data},target=/app/data",
                    image,
                )
                limits = json.loads(docker("inspect", "--format", "{{json .HostConfig}}", name))
                if (
                    limits["Memory"] != 2 * 1024**3
                    or limits["NanoCpus"] != 2_000_000_000
                    or limits["PidsLimit"] != 256
                ):
                    raise AssertionError("Container resource limits were not applied.")
                exercise(name, wait_ready(name, published_url(name)))
            finally:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)
                if os.name != "nt":
                    # Restore this temporary tree to the invoking host user before its cleanup.
                    docker(
                        "run",
                        "--rm",
                        "--user",
                        "0:0",
                        "--mount",
                        f"type=bind,source={data},target=/app/data",
                        image,
                        "python",
                        "-c",
                        "import os\n"
                        f"owner=({os.getuid()},{os.getgid()})\n"
                        "for root, dirs, files in os.walk('/app/data'):\n"
                        " os.chown(root,*owner)\n"
                        " for item in files:\n"
                        "  os.chown(os.path.join(root,item),*owner)\n",
                    )
        print("Docker context privacy, nonroot readiness, real MOBI, history, and restart passed.")
        return 0
    except subprocess.CalledProcessError as exc:
        print(exc.stdout)
        print(exc.stderr)
        raise
    finally:
        for container in (name, context_name):
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
        for built_image in (image, context_image):
            subprocess.run(
                ["docker", "image", "rm", "--force", built_image],
                capture_output=True,
                check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
