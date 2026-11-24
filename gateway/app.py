"""HTTP reverse proxy that routes every request with one config snapshot."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Iterable

import requests
from flask import Flask, Response, g, request
from requests.adapters import HTTPAdapter
from werkzeug.exceptions import HTTPException

from common.config import GatewayConfig, catalog_from_env
from gateway.config_store import ConfigStore

LOG = logging.getLogger("gateway")

ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Headers we never copy verbatim: either hop-by-hop, recomputed by requests /
# Flask, or invalid after the gateway decodes the upstream body.
RESPONSE_SKIP = HOP_BY_HOP | {
    "content-length",
    "content-encoding",
    "server",
    "date",
}

REQUEST_SKIP = HOP_BY_HOP | {"host", "content-length"}

RESERVED_PREFIX = "/__gateway"


def _build_session() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=64, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class _SessionPool:
    """One pooled requests.Session per worker thread (sessions are not shared)."""

    def __init__(self) -> None:
        self._local = threading.local()

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = _build_session()
            self._local.session = session
        return session


def _request_headers() -> dict[str, str]:
    connection_tokens = {
        token.strip().lower()
        for token in request.headers.get("Connection", "").split(",")
        if token.strip()
    }
    skip = REQUEST_SKIP | connection_tokens
    # Merge duplicate header lines (RFC 7230).  Cookie uses "; " as its
    # separator, every other repeated header uses ", ".
    merged: dict[str, str] = {}
    original_names: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in skip or lower == "accept-encoding":
            continue
        if lower in merged:
            separator = "; " if lower == "cookie" else ", "
            merged[lower] = merged[lower] + separator + value
        else:
            merged[lower] = value
            original_names[lower] = name
    headers = {original_names[lower]: value for lower, value in merged.items()}

    # Relay the upstream body uncompressed so bytes match our response headers.
    headers["Accept-Encoding"] = "identity"

    client_ip = request.remote_addr or ""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        headers["X-Forwarded-For"] = f"{forwarded_for}, {client_ip}"
    elif client_ip:
        headers["X-Forwarded-For"] = client_ip
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-Host"] = request.host
    headers["X-Request-Id"] = g.request_id
    return headers


def _raw_path() -> str:
    """Return the request target path exactly as the client sent it.

    ``request.path`` is percent-decoded, which loses distinctions such as
    ``%2F``; the upstream should receive the original octets.  Route matching
    still uses the decoded path so operators write plain paths.
    """
    raw_uri = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
    if not raw_uri:
        return request.path
    raw_path = raw_uri.split("?", 1)[0]
    return raw_path or "/"


def _response_headers(upstream: requests.Response) -> list[tuple[str, str]]:
    raw = upstream.raw
    connection_tokens = {
        token.strip().lower()
        for token in upstream.headers.get("Connection", "").split(",")
        if token.strip()
    }
    skip = RESPONSE_SKIP | connection_tokens
    result: list[tuple[str, str]] = []
    for name in list(raw.headers.keys()):
        if name.lower() in skip:
            continue
        for value in raw.headers.getlist(name):
            result.append((name, value))
    return result


def _json_response(
    status: int,
    payload: dict,
    config: GatewayConfig | None,
    *,
    backend: str | None = None,
) -> Response:
    response = Response(
        json.dumps(payload, ensure_ascii=False) + "\n",
        status=status,
        mimetype="application/json",
    )
    if config is not None:
        response.headers["X-Gateway-Config-Version"] = str(config.version)
    if backend:
        response.headers["X-Gateway-Backend"] = backend
    response.headers["X-Gateway-Request-Id"] = g.get("request_id", "")
    return response


def _json_error(
    status: int,
    code: str,
    message: str,
    config: GatewayConfig | None,
    *,
    path: str | None = None,
    backend: str | None = None,
) -> Response:
    payload = {"error": code, "message": message, "path": path}
    if backend:
        payload["backend"] = backend
    return _json_response(status, payload, config, backend=backend)


def _relay_body(upstream: requests.Response) -> Iterable[bytes]:
    try:
        for chunk in upstream.iter_content(chunk_size=16384):
            if chunk:
                yield chunk
    finally:
        upstream.close()


def _json_error_for_unhandled_exception(error: Exception) -> Response:
    LOG.exception("unhandled gateway error: %s", error)
    config: GatewayConfig | None = g.get("config_snapshot")
    return _json_error(
        500, "gateway_error", "网关内部错误", config, path=request.path
    )


def create_app() -> Flask:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.environ.get("MAX_REQUEST_BYTES", str(16 * 1024 * 1024))
    )
    catalog = catalog_from_env(os.environ.get("BACKEND_CATALOG"))
    store = ConfigStore(
        os.environ.get("CONFIG_PATH", "/data/routes.json"),
        catalog,
        reserved_prefixes=(RESERVED_PREFIX,),
    )
    read_timeout = float(os.environ.get("UPSTREAM_READ_TIMEOUT", "60"))
    connect_timeout = float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT", "3"))
    pool = _SessionPool()
    app.extensions["gateway_store"] = store
    app.extensions["gateway_pool"] = pool

    @app.before_request
    def prepare_request() -> None:
        g.request_id = uuid.uuid4().hex
        # Exactly one snapshot per request, taken before any backend work.
        # The object is immutable, so a publish during this request cannot
        # change where the request goes or which backend it reaches.
        g.config_snapshot = store.snapshot()

    @app.get(f"{RESERVED_PREFIX}/healthz")
    def healthz() -> Response:
        config: GatewayConfig | None = g.config_snapshot
        if config is None:
            return _json_error(503, "no_config", "网关尚未加载到有效配置", None)
        return _json_response(
            200, {"status": "ok", "active_version": config.version}, config
        )

    @app.get(f"{RESERVED_PREFIX}/status")
    def status() -> Response:
        config: GatewayConfig | None = g.config_snapshot
        payload = store.describe()
        payload["status"] = "ok" if config is not None else "no_config"
        if config is not None:
            payload["version"] = config.version
            payload["published_at"] = config.published_at
            payload["backends"] = [
                {"name": b.name, "url": b.url}
                for b in sorted(config.backends.values(), key=lambda i: i.name)
            ]
            payload["routes"] = [
                {"path": r.path, "match": r.match, "backend": r.backend}
                for r in config.routes
            ]
        return _json_response(200 if config is not None else 503, payload, config)

    @app.route("/", methods=ALL_METHODS)
    @app.route("/<path:path>", methods=ALL_METHODS)
    def proxy(path: str) -> Response:
        config: GatewayConfig | None = g.config_snapshot
        request_path = request.path
        if config is None:
            return _json_error(
                503, "no_config", "网关尚未加载到有效配置", None, path=request_path
            )

        # The control paths are never proxied, even when a "/" route exists.
        if request_path == RESERVED_PREFIX or request_path.startswith(
            RESERVED_PREFIX + "/"
        ):
            return _json_error(
                404, "route_not_found", "保留路径不可代理", config, path=request_path
            )

        # "." / ".." segments could let the upstream resolve the request to a
        # path that never matched the route table.  Reject instead of guessing.
        if any(segment in (".", "..") for segment in request_path.split("/")):
            return _json_error(
                400, "bad_path", "请求路径不能包含 . 或 .. 路径段", config, path=request_path
            )

        route = config.match(request_path)
        if route is None:
            return _json_error(
                404,
                "route_not_found",
                "没有匹配的路由（该路径可能已被撤下）",
                config,
                path=request_path,
            )

        backend = config.backends[route.backend]
        target = backend.url + _raw_path()
        if request.query_string:
            target += "?" + request.query_string.decode("latin-1")

        try:
            upstream = pool.get().request(
                request.method,
                target,
                headers=_request_headers(),
                data=request.get_data(cache=False),
                stream=True,
                allow_redirects=False,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.Timeout:
            LOG.warning("upstream timeout backend=%s target=%s", backend.name, target)
            return _json_error(
                504,
                "upstream_timeout",
                "后端响应超时",
                config,
                path=request_path,
                backend=backend.name,
            )
        except requests.RequestException as exc:
            LOG.warning(
                "upstream error backend=%s target=%s: %s", backend.name, target, exc
            )
            return _json_error(
                502,
                "upstream_error",
                "无法连接后端",
                config,
                path=request_path,
                backend=backend.name,
            )

        headers = _response_headers(upstream)
        if request.method == "HEAD" or upstream.status_code in (204, 304):
            upstream_length = upstream.headers.get("Content-Length")
            upstream.close()
            response = Response(b"", status=upstream.status_code)
            # A HEAD response must describe the entity a GET would return.
            if request.method == "HEAD" and upstream_length is not None:
                response.headers["Content-Length"] = upstream_length
        else:
            response = Response(_relay_body(upstream), status=upstream.status_code)
        # Replace Flask's defaults for single-valued headers (Content-Type) and
        # only append genuinely repeated headers such as Set-Cookie.
        replaced: set[str] = set()
        for name, value in headers:
            lower = name.lower()
            if lower in replaced:
                response.headers.add(name, value)
            else:
                response.headers[name] = value
                replaced.add(lower)
        response.headers["X-Gateway-Backend"] = backend.name
        response.headers["X-Gateway-Route"] = route.path
        response.headers["X-Gateway-Config-Version"] = str(config.version)
        response.headers["X-Gateway-Request-Id"] = g.request_id
        return response

    @app.errorhandler(413)
    def too_large(_error) -> Response:
        config: GatewayConfig | None = g.get("config_snapshot")
        return _json_error(
            413,
            "request_too_large",
            "请求体超过网关限制",
            config,
            path=request.path,
        )

    @app.errorhandler(Exception)
    def unhandled(error: Exception) -> Response:
        if isinstance(error, HTTPException):
            return _json_error(
                error.code or 500,
                (error.name or "http_error").lower().replace(" ", "_"),
                error.description or "请求无法处理",
                g.get("config_snapshot"),
                path=request.path,
            )
        return _json_error_for_unhandled_exception(error)

    return app


app = create_app()
