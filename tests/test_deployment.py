from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

from scripts import docker_smoke
from scripts.healthcheck import check_health, main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def health_server():
    servers = []

    def start(payload: bytes, status: int = 200):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path == "/health"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_container_readiness_uses_all_actual_subsystems(health_server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    address = health_server(json.dumps({"ok": True, "calibre": True, "database": True}).encode())
    assert check_health(address)


@pytest.mark.parametrize(
    "payload,status",
    [
        (b'{"ok":true,"database":true,"calibre":false}', 200),
        (b'{"ok":true,"calibre":true}', 200),
        (b'{"ok":1,"database":true,"calibre":true}', 200),
        (b"[]", 200),
        (b"not JSON", 200),
        (b'{"ok":true,"database":true,"calibre":true}', 503),
    ],
)
def test_readiness_rejects_unavailable_or_malformed_services(health_server, payload, status):
    assert not check_health(health_server(payload, status))


@pytest.mark.parametrize("port", ["not-a-port", "0", "65536"])
def test_healthcheck_rejects_invalid_port(monkeypatch, port):
    monkeypatch.setenv("KINDLE_SHELF_PORT", port)
    assert main() == 1


def test_compose_single_container_persists_data_and_matches_readiness():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert list(compose["services"]) == ["kindle-shelf"]
    service = compose["services"]["kindle-shelf"]
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "30s"
    assert service["mem_limit"] == "${KINDLE_SHELF_MEMORY_LIMIT:-2g}"
    assert service["cpus"] == "${KINDLE_SHELF_CPU_LIMIT:-2.0}"
    assert service["pids_limit"] == "${KINDLE_SHELF_PIDS_LIMIT:-256}"
    assert service["environment"]["KINDLE_SHELF_DATA"] == "/app/data"
    assert service["environment"]["KINDLE_SHELF_PORT"] == "8090"
    assert "KINDLE_SHELF_PUBLIC_URL" in service["environment"]
    assert service["environment"]["KINDLE_SHELF_TIMEZONE"] == (
        "${KINDLE_SHELF_TIMEZONE:-Asia/Shanghai}"
    )
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": "./data",
            "target": "/app/data",
            "bind": {"create_host_path": False},
        }
    ]
    assert service["healthcheck"]["test"] == ["CMD", "python", "scripts/healthcheck.py"]


def test_dependency_lock_has_exact_versions_and_hashes_for_every_package():
    for filename in ("requirements.txt", "requirements-dev.txt"):
        logical_lines = (
            (ROOT / filename).read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
        )
        found = 0
        for line in logical_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            requirement, *hashes = stripped.split(" --hash=")
            parsed = Requirement(requirement.strip())
            assert len(parsed.specifier) == 1, f"Not pinned: {requirement}"
            specifier = next(iter(parsed.specifier))
            assert specifier.operator == "==", f"Not exact: {requirement}"
            assert "*" not in specifier.version, f"Wildcard pin: {requirement}"
            assert hashes and all(item.startswith("sha256:") for item in hashes)
            found += 1
        assert found >= 10, f"Unexpectedly incomplete dependency lock: {filename}"


def test_ci_runs_real_platform_and_container_regressions():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    matrix = workflow["jobs"]["regression"]["strategy"]["matrix"]
    assert set(matrix["os"]) == {"windows-latest", "ubuntu-latest"}
    assert set(matrix["python"]) == {"3.10", "3.12"}
    docker_steps = workflow["jobs"]["docker"]["steps"]
    assert any(step.get("run") == "python scripts/docker_smoke.py" for step in docker_steps)
    assert workflow["permissions"] == {"contents": "read"}


def test_smoke_restart_uses_reassigned_published_port(monkeypatch):
    mobi = b"\x00" * 60 + b"BOOKMOBI" + b"\x00" * 1500
    history = [{"event_type": "status_changed"}]
    restarted = False
    port_queried = False

    class FakeClient:
        base_url = "http://127.0.0.1:31000"

        def post(self, *_args):
            pass

        def json(self, path):
            if path == "/api/books":
                return {
                    "books": [{"id": "smoke", "filename": "article.mobi", "status": "finished"}]
                }
            if path == "/api/stats":
                return {"finished": 1, "downloads": 1}
            assert path == "/api/history"
            return {"events": history}

        def get(self, path):
            return b"<html>Kindle shelf</html>" if path == "/kindle" else mobi

    def fake_docker(*arguments):
        nonlocal restarted, port_queried
        if arguments[0] == "inspect":
            return '{"Running":false,"ExitCode":0}'
        if arguments[0] == "start":
            restarted = True
        if arguments[0] == "port":
            assert restarted
            port_queried = True
            return "127.0.0.1:32000"
        return ""

    def fake_ready(name, base_url):
        assert name == "test-container"
        assert base_url == "http://127.0.0.1:32000"
        return FakeClient()

    monkeypatch.setattr(docker_smoke, "docker", fake_docker)
    monkeypatch.setattr(docker_smoke, "wait_ready", fake_ready)
    docker_smoke.exercise("test-container", FakeClient())
    assert restarted and port_queried


def test_smoke_readiness_recovers_from_transient_connection_refusal(monkeypatch):
    probes = []
    waits = []

    class ProbeClient:
        def __init__(self, base_url):
            self.base_url = base_url

        def json(self, path):
            probes.append(path)
            if len(probes) == 1:
                raise urllib.error.URLError("connection refused")
            return {"ok": True, "database": True, "calibre": True}

    monkeypatch.setattr(docker_smoke, "Client", ProbeClient)
    monkeypatch.setattr(
        docker_smoke,
        "docker",
        lambda *_args: '{"Running":true,"Health":{"Status":"healthy"}}',
    )
    monkeypatch.setattr(docker_smoke.time, "sleep", waits.append)
    client = docker_smoke.wait_ready("test-container", "http://127.0.0.1:32000")
    assert client.base_url == "http://127.0.0.1:32000"
    assert probes == ["/health", "/health"]
    assert waits == [1]


def test_smoke_readiness_timeout_keeps_deadline_and_error_diagnosis(monkeypatch):
    clock = [0]
    probes = []

    class RefusedClient:
        def __init__(self, _base_url):
            pass

        def json(self, _path):
            probes.append(clock[0])
            raise urllib.error.URLError("connection refused at published port")

    def fake_docker(*arguments):
        if arguments[0] == "logs":
            return "application log evidence"
        return '{"Running":true,"Health":{"Status":"healthy"}}'

    def advance(_seconds):
        clock[0] += 60

    monkeypatch.setattr(docker_smoke, "Client", RefusedClient)
    monkeypatch.setattr(docker_smoke, "docker", fake_docker)
    monkeypatch.setattr(docker_smoke.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(docker_smoke.time, "sleep", advance)
    with pytest.raises(AssertionError) as error:
        docker_smoke.wait_ready("test-container", "http://127.0.0.1:32000")
    assert probes == [0, 60, 120]
    assert clock[0] == 180
    assert "connection refused at published port" in str(error.value)
    assert "application log evidence" in str(error.value)


def test_smoke_readiness_fails_immediately_when_container_exits(monkeypatch):
    commands = []

    def fake_docker(*arguments):
        commands.append(arguments[0])
        return "process failure evidence" if arguments[0] == "logs" else '{"Running":false}'

    monkeypatch.setattr(docker_smoke, "docker", fake_docker)
    with pytest.raises(AssertionError, match="Container exited: process failure evidence"):
        docker_smoke.wait_ready("test-container", "http://127.0.0.1:32000")
    assert commands == ["inspect", "logs"]
