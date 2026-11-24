from __future__ import annotations

import json
import threading
import time

import pytest
import requests as http
from flask import Flask, Response, request
from werkzeug.serving import make_server

from common.config import parse_config
from common.storage import atomic_write_json
from gateway.app import create_app


class ThreadedServer:
    def __init__(self, app, host="127.0.0.1", port=0):
        self.server = make_server(host, port, app, threaded=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


def make_backend(name: str) -> Flask:
    app = Flask(f"backend-{name}")

    @app.get("/")
    @app.get("/<path:path>")
    @app.post("/<path:path>")
    def echo(path: str = ""):
        try:
            delay = int(request.args.get("delay_ms", "0"))
        except ValueError:
            delay = 0
        time.sleep(max(0, min(delay, 10000)) / 1000.0)
        response = Response(
            json.dumps({"service": name, "path": "/" + path}),
            mimetype="application/json",
        )
        response.headers["X-Sample-Service"] = name
        return response

    return app


@pytest.fixture()
def backends():
    with ThreadedServer(make_backend("sample-a")) as a, ThreadedServer(
        make_backend("sample-b")
    ) as b:
        yield {"sample-a": a.url, "sample-b": b.url}


@pytest.fixture()
def gateway(tmp_path, backends, monkeypatch):
    config_path = tmp_path / "routes.json"
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    # No Compose catalog here: gateway tests may point at arbitrary test URLs.
    monkeypatch.delenv("BACKEND_CATALOG", raising=False)
    app = create_app()
    with ThreadedServer(app) as server:
        yield server, config_path, backends


def write_config(path, backends, routes, version=1):
    data = {
        "version": version,
        "published_at": "2026-01-01T00:00:00+00:00",
        "backends": [
            {"name": name, "url": url} for name, url in backends.items()
        ],
        "routes": routes,
    }
    parse_config(data, catalog=backends)
    atomic_write_json(str(path), data)


def test_request_is_forwarded_and_labelled(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.get(server.url + "/a/x", timeout=10)
    assert response.status_code == 200
    assert response.json()["service"] == "sample-a"
    assert response.headers["X-Gateway-Backend"] == "sample-a"
    assert response.headers["X-Gateway-Route"] == "/a"
    assert response.headers["X-Gateway-Config-Version"] == "1"


def test_unmatched_path_returns_404(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.get(server.url + "/removed", timeout=10)
    assert response.status_code == 404
    assert response.json()["error"] == "route_not_found"
    assert "X-Gateway-Backend" not in response.headers


def test_in_flight_request_survives_route_removal(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/slow", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    results = {}

    def slow_call():
        results["response"] = http.get(
            server.url + "/slow?delay_ms=2000", timeout=30
        )

    thread = threading.Thread(target=slow_call)
    thread.start()
    time.sleep(0.5)  # request is now being handled by sample-a
    write_config(
        path,
        backends,
        [{"path": "/other", "match": "prefix", "backend": "sample-b"}],
        version=2,
    )
    # A new request after the publish sees the removal immediately...
    new_response = http.get(server.url + "/slow", timeout=10)
    assert new_response.status_code == 404
    # ...while the in-flight request completes against its original snapshot.
    thread.join(timeout=30)
    response = results["response"]
    assert response.status_code == 200
    assert response.json()["service"] == "sample-a"
    assert response.headers["X-Gateway-Backend"] == "sample-a"
    assert response.headers["X-Gateway-Config-Version"] == "1"


def test_in_flight_request_keeps_old_backend_after_repoint(gateway):
    """A publish that repoints a route must not affect a request already sent."""
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/slow", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    results = {}

    def slow_call():
        results["response"] = http.get(
            server.url + "/slow?delay_ms=1500", timeout=30
        )

    thread = threading.Thread(target=slow_call)
    thread.start()
    time.sleep(0.4)
    write_config(
        path,
        backends,
        [{"path": "/slow", "match": "prefix", "backend": "sample-b"}],
        version=2,
    )
    fresh = http.get(server.url + "/slow", timeout=10)
    assert fresh.headers["X-Gateway-Backend"] == "sample-b"
    assert fresh.headers["X-Gateway-Config-Version"] == "2"

    thread.join(timeout=30)
    response = results["response"]
    assert response.status_code == 200
    assert response.json()["service"] == "sample-a"
    assert response.headers["X-Gateway-Backend"] == "sample-a"
    assert response.headers["X-Gateway-Config-Version"] == "1"


def test_bad_config_file_keeps_serving_old_config(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    first = http.get(server.url + "/a", timeout=10)
    assert first.status_code == 200
    assert first.headers["X-Gateway-Config-Version"] == "1"

    path.write_text("{ broken json", encoding="utf-8")
    second = http.get(server.url + "/a", timeout=10)
    assert second.status_code == 200
    assert second.headers["X-Gateway-Config-Version"] == "1"

    status = http.get(server.url + "/__gateway/status", timeout=10).json()
    assert status["active_version"] == 1
    assert status["config_file_error"]


def test_config_with_dangling_backend_reference_is_rejected(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
        version=1,
    )
    assert http.get(server.url + "/a", timeout=10).headers[
        "X-Gateway-Config-Version"
    ] == "1"
    broken = {
        "version": 2,
        "backends": [{"name": n, "url": u} for n, u in backends.items()],
        "routes": [{"path": "/a", "match": "prefix", "backend": "ghost"}],
    }
    atomic_write_json(str(path), broken)
    response = http.get(server.url + "/a", timeout=10)
    assert response.headers["X-Gateway-Config-Version"] == "1"

    status = http.get(server.url + "/__gateway/status", timeout=10).json()
    assert status["active_version"] == 1
    assert "ghost" in status["config_file_error"]


def test_catalog_pinning_rejects_unknown_backend(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.json"
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv(
        "BACKEND_CATALOG", '{"sample-a": "http://127.0.0.1:9"}'
    )
    app = create_app()
    with ThreadedServer(app) as server:
        broken = {
            "version": 1,
            "backends": [{"name": "rogue", "url": "http://127.0.0.1:9"}],
            "routes": [{"path": "/a", "match": "prefix", "backend": "rogue"}],
        }
        atomic_write_json(str(config_path), broken)
        response = http.get(server.url + "/a", timeout=10)
        assert response.status_code == 503
        assert response.json()["error"] == "no_config"


def test_healthz_and_reserved_prefix(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    health = http.get(server.url + "/__gateway/healthz", timeout=10)
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_upstream_failure_returns_502(gateway):
    server, path, _backends = gateway
    write_config(
        path,
        {"sample-a": "http://127.0.0.1:1"},
        [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    )
    response = http.get(server.url + "/a", timeout=10)
    assert response.status_code == 502
    assert response.headers["X-Gateway-Backend"] == "sample-a"


def test_query_string_and_method_forwarded(gateway):
    server, path, backends = gateway
    write_config(
        path,
        backends,
        [{"path": "/a", "match": "prefix", "backend": "sample-b"}],
    )
    response = http.post(server.url + "/a/deep?x=1&y=2", data=b"hi", timeout=10)
    assert response.status_code == 200
    assert response.headers["X-Gateway-Backend"] == "sample-b"
