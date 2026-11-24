"""Publishing UI for the container gateway.

The publisher validates a complete routing table, commits it atomically to the
shared configuration volume and triggers delayed probe requests through the
gateway so operators can see which backend served a response.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests
from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from common.config import (
    MAX_BACKENDS,
    MAX_ROUTES,
    SCHEMA_VERSION,
    ConfigValidationError,
    GatewayConfig,
    catalog_from_env,
    parse_config,
    read_config_file,
    utc_now_iso,
)
from common.storage import ConfigWriteError, atomic_write_json, read_version

LOG = logging.getLogger("publisher")

RESERVED_PREFIXES = ("/__gateway",)
MAX_PROBE_SECONDS = 120.0
PROBE_JOB_TTL = 900.0


@dataclass
class ProbeJob:
    job_id: str
    method: str
    path: str
    delay_ms: int
    created_at: float = field(default_factory=time.time)
    state: str = "pending"
    status_code: int | None = None
    backend: str | None = None
    route: str | None = None
    config_version: str | None = None
    elapsed_ms: int | None = None
    body: str | None = None
    error: str | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "method": self.method,
            "path": self.path,
            "delay_ms": self.delay_ms,
            "state": self.state,
            "status_code": self.status_code,
            "backend": self.backend,
            "route": self.route,
            "config_version": self.config_version,
            "elapsed_ms": self.elapsed_ms,
            "body": self.body,
            "error": self.error,
        }


class ProbeRegistry:
    """Small in-memory registry of delayed probe requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, ProbeJob] = {}

    def create(self, method: str, path: str, delay_ms: int) -> ProbeJob:
        job = ProbeJob(
            job_id=uuid.uuid4().hex[:12],
            method=method,
            path=path,
            delay_ms=delay_ms,
        )
        with self._lock:
            self._prune_locked()
            self._jobs[job.job_id] = job
        return job

    def update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in changes.items():
                setattr(job, key, value)

    def get(self, job_id: str) -> ProbeJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job is not None else None

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(), key=lambda item: item.created_at, reverse=True
            )
            return [job.to_dict() for job in jobs[:limit]]

    def _prune_locked(self) -> None:
        cutoff = time.time() - PROBE_JOB_TTL
        stale = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for job_id in stale:
            self._jobs.pop(job_id, None)


def _form_rows(form) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    backend_names = form.getlist("backend_name")
    backend_urls = form.getlist("backend_url")
    backends = [
        {"name": name.strip(), "url": url.strip()}
        for name, url in zip(backend_names, backend_urls)
        if name.strip() or url.strip()
    ]
    route_paths = form.getlist("route_path")
    route_matches = form.getlist("route_match")
    route_backends = form.getlist("route_backend")
    routes = [
        {
            "path": path.strip(),
            "match": (match or "prefix").strip(),
            "backend": (backend or "").strip(),
        }
        for path, match, backend in zip(route_paths, route_matches, route_backends)
        if path.strip() or backend.strip()
    ]
    return backends, routes


def _parse_probe_input(form) -> tuple[str, str, int]:
    path = (form.get("probe_path") or "/").strip()
    method = (form.get("probe_method") or "GET").strip().upper()
    delay_raw = (form.get("probe_delay") or "3").strip()
    if not path.startswith("/"):
        raise ValueError("试请求路径必须以 / 开头")
    if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        raise ValueError("不支持的试请求方法")
    try:
        delay_ms = int(float(delay_raw) * 1000)
    except ValueError as exc:
        raise ValueError("延时必须是数字（秒）") from exc
    if delay_ms < 0 or delay_ms > MAX_PROBE_SECONDS * 1000:
        raise ValueError(f"延时必须在 0 到 {int(MAX_PROBE_SECONDS)} 秒之间")
    return method, path, delay_ms


def _run_probe(
    registry: ProbeRegistry,
    job: ProbeJob,
    gateway_url: str,
    timeout: float,
) -> None:
    registry.update(job.job_id, state="running")
    query = f"delay_ms={job.delay_ms}"
    url = gateway_url.rstrip("/") + job.path
    url += ("&" if "?" in url else "?") + query
    started = time.monotonic()
    try:
        response = requests.request(
            job.method,
            url,
            timeout=(5.0, timeout),
            allow_redirects=False,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = response.text[:4000]
        registry.update(
            job.job_id,
            state="done",
            status_code=response.status_code,
            backend=response.headers.get("X-Gateway-Backend"),
            route=response.headers.get("X-Gateway-Route"),
            config_version=response.headers.get("X-Gateway-Config-Version"),
            elapsed_ms=elapsed_ms,
            body=body,
            finished_at=time.time(),
        )
    except requests.RequestException as exc:
        registry.update(
            job.job_id,
            state="error",
            error=str(exc),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            finished_at=time.time(),
        )


def _active_config(
    path: str, catalog: dict[str, str] | None = None
) -> tuple[GatewayConfig | None, str | None]:
    """Read and validate the config currently on the shared volume."""
    if not os.path.exists(path):
        return None, "共享卷中还没有配置文件"
    try:
        return (
            read_config_file(
                path,
                catalog=catalog or None,
                reserved_prefixes=RESERVED_PREFIXES,
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - displayed to the operator
        return None, str(exc)


def _gateway_status(gateway_url: str, timeout: float = 2.0) -> dict[str, Any]:
    try:
        response = requests.get(
            gateway_url.rstrip("/") + "/__gateway/status", timeout=timeout
        )
        payload = response.json()
        payload["_http_status"] = response.status_code
        return payload
    except (requests.RequestException, ValueError) as exc:
        return {"_error": str(exc)}


def create_app() -> Flask:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    app = Flask(__name__)
    config_path = os.environ.get("CONFIG_PATH", "/data/routes.json")
    gateway_url = os.environ.get("GATEWAY_URL", "http://gateway:8000")
    probe_timeout = float(os.environ.get("PROBE_TIMEOUT", "120"))
    catalog = catalog_from_env(os.environ.get("BACKEND_CATALOG"))
    labels = json.loads(os.environ.get("BACKEND_LABELS", "{}") or "{}")
    registry = ProbeRegistry()

    app.config["CONFIG_PATH"] = config_path
    app.config["GATEWAY_URL"] = gateway_url
    app.config["CATALOG"] = catalog
    app.config["LABELS"] = labels
    app.config["PROBES"] = registry

    @app.before_request
    def before_request() -> None:
        g.request_id = uuid.uuid4().hex

    @app.get("/")
    def index() -> Response:
        config, config_error = _active_config(config_path, catalog)
        gateway = _gateway_status(gateway_url)
        form_backends: list[dict[str, str]]
        form_routes: list[dict[str, str]]
        if config is not None:
            form_backends = [
                {"name": b.name, "url": b.url}
                for b in sorted(config.backends.values(), key=lambda i: i.name)
            ]
            form_routes = [
                {"path": r.path, "match": r.match, "backend": r.backend}
                for r in config.routes
            ]
        else:
            form_backends = [
                {"name": name, "url": url} for name, url in sorted(catalog.items())
            ]
            form_routes = [
                {"path": f"/{name}", "match": "prefix", "backend": name}
                for name, _url in sorted(catalog.items())
            ]
        published = request.args.get("published")
        return render_template(
            "index.html",
            catalog=catalog,
            labels=labels,
            config=config,
            config_path=config_path,
            config_error=config_error,
            gateway=gateway,
            gateway_url=gateway_url,
            form_backends=form_backends,
            form_routes=form_routes,
            has_config=config is not None,
            published=published,
            max_backends=MAX_BACKENDS,
            max_routes=MAX_ROUTES,
        )

    @app.post("/publish")
    def publish() -> Response:
        config, config_error = _active_config(config_path, catalog)
        gateway = _gateway_status(gateway_url)
        backends, routes = _form_rows(request.form)
        published_at = utc_now_iso()
        current_version = read_version(config_path) or 0
        payload = {
            "schema_version": SCHEMA_VERSION,
            "version": current_version + 1,
            "published_at": published_at,
            "backends": backends,
            "routes": routes,
        }
        errors: list[str] = []
        try:
            candidate = parse_config(
                payload,
                catalog=catalog or None,
                reserved_prefixes=RESERVED_PREFIXES,
            )
        except ConfigValidationError as exc:
            errors = exc.errors
            candidate = None

        if errors:
            return (
                render_template(
                    "index.html",
                    catalog=catalog,
                    labels=labels,
                    config=config,
                    config_path=config_path,
                    config_error=config_error,
                    gateway=gateway,
                    gateway_url=gateway_url,
                    form_backends=backends,
                    form_routes=routes,
                    has_config=config is not None,
                    published=None,
                    errors=errors,
                            max_backends=MAX_BACKENDS,
                    max_routes=MAX_ROUTES,
                ),
                400,
            )

        assert candidate is not None
        lock_path = os.path.join(
            os.path.dirname(os.path.abspath(config_path)), ".publish.lock"
        )
        try:
            os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
            with open(lock_path, "a+") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                # Re-read inside the lock so concurrent publishers never reuse
                # a version number.
                locked_version = read_version(config_path) or 0
                if locked_version + 1 != candidate.version:
                    candidate = parse_config(
                        {**candidate.to_dict(), "version": locked_version + 1},
                        catalog=catalog or None,
                        reserved_prefixes=RESERVED_PREFIXES,
                    )
                atomic_write_json(config_path, candidate.to_dict())
        except (OSError, ConfigWriteError, ConfigValidationError) as exc:
            LOG.exception("publish failed")
            errors = [f"配置写入失败，现役配置未变更：{exc}"]
            return (
                render_template(
                    "index.html",
                    catalog=catalog,
                    labels=labels,
                    config=config,
                    config_path=config_path,
                    config_error=config_error,
                    gateway=gateway,
                    gateway_url=gateway_url,
                    form_backends=backends,
                    form_routes=routes,
                    has_config=config is not None,
                    published=None,
                    errors=errors,
                            max_backends=MAX_BACKENDS,
                    max_routes=MAX_ROUTES,
                ),
                500,
            )

        LOG.info(
            "published configuration version=%s routes=%d backends=%d",
            candidate.version,
            len(candidate.routes),
            len(candidate.backends),
        )
        return redirect(url_for("index", published=candidate.version))

    @app.post("/probe")
    def probe() -> Response:
        try:
            method, path, delay_ms = _parse_probe_input(request.form)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        job = registry.create(method, path, delay_ms)
        thread = threading.Thread(
            target=_run_probe,
            args=(registry, job, gateway_url, probe_timeout),
            name=f"probe-{job.job_id}",
            daemon=True,
        )
        thread.start()
        return jsonify({"job_id": job.job_id}), 202

    @app.get("/probe/<job_id>")
    def probe_status(job_id: str) -> Response:
        payload = registry.snapshot(job_id)
        if payload is None:
            abort(404)
        return jsonify(payload)

    @app.get("/probe-recent")
    def probe_recent() -> Response:
        return jsonify(
            {"recent": registry.recent(limit=20)}
        )

    @app.get("/healthz")
    def healthz() -> Response:
        active, error = _active_config(config_path, catalog)
        if active is None:
            return jsonify({"status": "degraded", "config_error": error}), 503
        return jsonify({"status": "ok", "active_version": active.version})

    return app


app = create_app()
