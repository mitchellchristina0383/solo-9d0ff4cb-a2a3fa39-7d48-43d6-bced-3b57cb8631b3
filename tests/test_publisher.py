from __future__ import annotations

import json
import os
import threading
import time

import pytest
import requests as http
from flask import Flask, Response, request
from werkzeug.serving import make_server

from common.config import parse_config
from common.storage import atomic_write_json
from publisher.app import create_app

CATALOG = {
    "sample-a": "http://127.0.0.1:9",
    "sample-b": "http://127.0.0.1:9",
}


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


def fake_gateway():
    app = Flask("fake-gateway")

    @app.get("/__gateway/status")
    def status():
        return Response(
            json.dumps(
                {
                    "status": "ok",
                    "active_version": 1,
                    "route_count": 1,
                    "config_file_error": None,
                }
            ),
            mimetype="application/json",
        )

    @app.get("/<path:path>")
    @app.get("/")
    def echo(path: str = ""):
        try:
            delay = int(request.args.get("delay_ms", "0"))
        except ValueError:
            delay = 0
        time.sleep(max(0, min(delay, 5000)) / 1000.0)
        response = Response(
            json.dumps({"gateway_path": "/" + path}), mimetype="application/json"
        )
        response.headers["X-Gateway-Backend"] = "sample-a"
        response.headers["X-Gateway-Route"] = "/probe"
        response.headers["X-Gateway-Config-Version"] = "7"
        return response

    return app


@pytest.fixture()
def publisher(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.json"
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BACKEND_CATALOG", json.dumps(CATALOG))
    monkeypatch.setenv("BACKEND_LABELS", json.dumps({"sample-a": "样例服务 A"}))
    with ThreadedServer(fake_gateway()) as gw:
        monkeypatch.setenv("GATEWAY_URL", gw.url)
        with ThreadedServer(create_app()) as pub:
            yield pub, config_path


def seed(path, routes=None, version=1):
    data = {
        "version": version,
        "published_at": "2026-01-01T00:00:00+00:00",
        "backends": [{"name": n, "url": u} for n, u in CATALOG.items()],
        "routes": routes
        or [{"path": "/a", "match": "prefix", "backend": "sample-a"}],
    }
    parse_config(data, catalog=CATALOG)
    atomic_write_json(str(path), data)


def publish_form(backends, routes):
    data = []
    for name, url in backends:
        data.append(("backend_name", name))
        data.append(("backend_url", url))
    for route in routes:
        data.append(("route_path", route["path"]))
        data.append(("route_match", route.get("match", "prefix")))
        data.append(("route_backend", route["backend"]))
    return data


def test_index_lists_catalog(publisher):
    server, path = publisher
    page = http.get(server.url + "/", timeout=10)
    assert page.status_code == 200
    assert "样例服务 A" in page.text


def test_publish_valid_config_writes_new_version(publisher):
    server, path = publisher
    seed(path, version=1)
    response = http.post(
        server.url + "/publish",
        data=publish_form(
            list(CATALOG.items()),
            [{"path": "/b", "match": "prefix", "backend": "sample-b"}],
        ),
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["routes"] == [
        {"path": "/b", "match": "prefix", "backend": "sample-b"}
    ]


def test_publish_route_with_unknown_backend_rejected(publisher):
    server, path = publisher
    seed(path, version=1)
    before = path.read_text(encoding="utf-8")
    response = http.post(
        server.url + "/publish",
        data=publish_form(
            list(CATALOG.items()),
            [{"path": "/x", "match": "prefix", "backend": "ghost"}],
        ),
        timeout=10,
    )
    assert response.status_code == 400
    assert "ghost" in response.text
    assert path.read_text(encoding="utf-8") == before


def test_publish_duplicate_paths_rejected(publisher):
    server, path = publisher
    seed(path, version=1)
    before = path.read_text(encoding="utf-8")
    response = http.post(
        server.url + "/publish",
        data=publish_form(
            list(CATALOG.items()),
            [
                {"path": "/x", "match": "prefix", "backend": "sample-a"},
                {"path": "/x", "match": "exact", "backend": "sample-b"},
            ],
        ),
        timeout=10,
    )
    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == before


def test_publish_write_failure_leaves_old_file(publisher, tmp_path):
    server, path = publisher
    seed(path, version=1)
    before = path.read_text(encoding="utf-8")
    # Make the directory read-only so the temp file cannot be created.
    os.chmod(tmp_path, 0o500)
    try:
        response = http.post(
            server.url + "/publish",
            data=publish_form(
                list(CATALOG.items()),
                [{"path": "/x", "match": "prefix", "backend": "sample-a"}],
            ),
            timeout=10,
        )
    finally:
        os.chmod(tmp_path, 0o700)
    assert response.status_code == 500
    assert "写入失败" in response.text
    assert path.read_text(encoding="utf-8") == before


def test_publish_then_empty_table_removes_all_routes(publisher):
    server, path = publisher
    seed(path, version=1)
    response = http.post(
        server.url + "/publish",
        data=publish_form([("sample-a", CATALOG["sample-a"])], []),
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["routes"] == []
    assert data["version"] == 2


def test_probe_reports_backend_and_config_version(publisher):
    server, path = publisher
    seed(path, version=1)
    response = http.post(
        server.url + "/probe",
        data={"probe_method": "GET", "probe_path": "/probe/x", "probe_delay": "0.2"},
        timeout=10,
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    deadline = time.time() + 10
    job = None
    while time.time() < deadline:
        job = http.get(server.url + f"/probe/{job_id}", timeout=10).json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job is not None
    assert job["state"] == "done"
    assert job["backend"] == "sample-a"
    assert job["config_version"] == "7"
    assert job["route"] == "/probe"
    assert job["status_code"] == 200


def test_probe_rejects_bad_input(publisher):
    server, _path = publisher
    response = http.post(
        server.url + "/probe",
        data={"probe_path": "no-slash", "probe_delay": "1"},
        timeout=10,
    )
    assert response.status_code == 400
