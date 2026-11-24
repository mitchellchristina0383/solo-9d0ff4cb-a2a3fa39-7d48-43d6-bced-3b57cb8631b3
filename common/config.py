"""Validation and immutable model for the gateway routing configuration.

Both the publisher and the gateway validate with this module.  The publisher
validates before it writes (so invalid configuration never becomes active),
while the gateway validates everything it reads (so a broken file left behind
by an external writer, a failed write or a partial deployment can never replace
the currently active configuration).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
MAX_BACKENDS = 50
MAX_ROUTES = 200

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PATH_RE = re.compile(r"^/[^\s?#\\]*$")
_MATCH_TYPES = ("prefix", "exact")


class ConfigValidationError(ValueError):
    """Raised with a list of human readable validation errors."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class ConfigLoadError(ValueError):
    """Raised when a configuration file cannot be read or parsed."""


@dataclass(frozen=True)
class Backend:
    name: str
    url: str


@dataclass(frozen=True)
class Route:
    path: str
    backend: str
    match: str = "prefix"


@dataclass(frozen=True)
class GatewayConfig:
    """A complete, immutable configuration snapshot.

    A request keeps a reference to exactly one of these objects for its whole
    lifetime.  Publishing a new configuration only swaps the reference held by
    the store; it never mutates an object an in-flight request is using.
    """

    version: int
    published_at: str
    backends: Mapping[str, Backend]
    routes: tuple[Route, ...]
    schema_version: int = SCHEMA_VERSION
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def match(self, path: str) -> Route | None:
        """Return the most specific route for *path* (longest path wins)."""
        for route in self.routes:
            base = route.path.rstrip("/")
            if route.match == "exact":
                if path == route.path:
                    return route
            elif base == "":
                # A prefix route on "/" matches every absolute path.
                if path.startswith("/"):
                    return route
            elif path == base or path.startswith(base + "/"):
                return route
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "version": self.version,
            "published_at": self.published_at,
            "backends": [
                {"name": b.name, "url": b.url}
                for b in sorted(self.backends.values(), key=lambda item: item.name)
            ],
            "routes": [
                {"path": r.path, "match": r.match, "backend": r.backend}
                for r in self.routes
            ],
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_backend_url(value: Any) -> str:
    """Validate an http(s) base URL and return its canonical form."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("后端地址必须是非空字符串")
    url = value.strip()
    if any(ch.isspace() for ch in url):
        raise ValueError("后端地址不能包含空白字符")
    try:
        parts = urlsplit(url)
        # Accessing .port raises ValueError for invalid ports.
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"后端地址不合法：{exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError("后端地址只支持 http/https")
    if not parts.hostname:
        raise ValueError("后端地址缺少主机名")
    if parts.username or parts.password:
        raise ValueError("后端地址不允许包含用户名或密码")
    if parts.query or parts.fragment:
        raise ValueError("后端地址不能包含查询串或片段")
    del port
    return url.rstrip("/")


def normalize_catalog(catalog: Mapping[str, str] | None) -> dict[str, str]:
    """Normalize the Compose service catalog (name -> base URL)."""
    if not catalog:
        return {}
    normalized: dict[str, str] = {}
    for name, url in catalog.items():
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError(f"样例服务名不合法：{name!r}")
        try:
            normalized[name] = normalize_backend_url(url)
        except ValueError as exc:
            raise ValueError(f"样例服务 {name!r} 的地址不合法：{exc}") from exc
    return normalized


def catalog_from_env(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"BACKEND_CATALOG 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("BACKEND_CATALOG 必须是 {服务名: 地址} 对象")
    return normalize_catalog(raw)


def parse_config(
    data: Any,
    *,
    catalog: Mapping[str, str] | None = None,
    reserved_prefixes: tuple[str, ...] = ("/__gateway",),
) -> GatewayConfig:
    """Validate *data* and return an immutable GatewayConfig.

    ``catalog`` restricts backend names to Compose sample services and pins
    each name to the address declared in Compose.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ConfigValidationError(["配置根节点必须是 JSON 对象"])

    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        errors.append("version 必须是大于 0 的整数")
    published_at = data.get("published_at", "")
    if not isinstance(published_at, str):
        errors.append("published_at 必须是字符串")

    catalog_norm = normalize_catalog(catalog) if catalog else {}

    raw_backends = data.get("backends")
    if not isinstance(raw_backends, list):
        errors.append("backends 必须是数组")
        raw_backends = []
    if len(raw_backends) > MAX_BACKENDS:
        errors.append(f"后端数量不能超过 {MAX_BACKENDS}")
        raw_backends = raw_backends[:MAX_BACKENDS]

    backends: dict[str, Backend] = {}
    for index, item in enumerate(raw_backends):
        label = f"后端第 {index + 1} 项"
        if not isinstance(item, dict):
            errors.append(f"{label}必须是对象")
            continue
        name = item.get("name")
        url = item.get("url")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            errors.append(f"{label}的 name 不合法（小写字母开头，可含数字和 -）")
            continue
        if name in backends:
            errors.append(f"后端名称重复：{name}")
            continue
        try:
            clean_url = normalize_backend_url(url)
        except ValueError as exc:
            errors.append(f"后端 {name}：{exc}")
            continue
        if catalog_norm:
            if name not in catalog_norm:
                errors.append(f"后端 {name} 不在 Compose 样例服务列表中")
                continue
            if clean_url != catalog_norm[name]:
                errors.append(
                    f"后端 {name} 的地址必须与 Compose 样例服务一致："
                    f"{catalog_norm[name]}"
                )
                continue
        backends[name] = Backend(name=name, url=clean_url)

    raw_routes = data.get("routes")
    if not isinstance(raw_routes, list):
        errors.append("routes 必须是数组")
        raw_routes = []
    if len(raw_routes) > MAX_ROUTES:
        errors.append(f"路由数量不能超过 {MAX_ROUTES}")
        raw_routes = raw_routes[:MAX_ROUTES]

    routes: list[Route] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(raw_routes):
        label = f"路由第 {index + 1} 项"
        if not isinstance(item, dict):
            errors.append(f"{label}必须是对象")
            continue
        path = item.get("path")
        backend = item.get("backend")
        match = item.get("match", "prefix")
        if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
            errors.append(f"{label}的 path 必须是以 / 开头的绝对路径")
            continue
        if any(segment in (".", "..") for segment in path.split("/")):
            errors.append(f"{label}的 path 不能包含 . 或 .. 路径段")
            continue
        if any(path == prefix or path.startswith(prefix.rstrip("/") + "/")
               for prefix in reserved_prefixes):
            errors.append(f"{label}的 path 不能占用保留前缀 {reserved_prefixes}")
            continue
        if match not in _MATCH_TYPES:
            errors.append(f"{label}的 match 只能是 prefix 或 exact")
            continue
        if not isinstance(backend, str) or not backend:
            errors.append(f"{label}的 backend 不能为空")
            continue
        if backend not in backends:
            errors.append(f"{label}引用了不存在的后端：{backend}")
            continue
        if path in seen_paths:
            errors.append(f"路由路径重复：{path}")
            continue
        seen_paths.add(path)
        routes.append(Route(path=path, backend=backend, match=match))

    if errors:
        raise ConfigValidationError(errors)

    routes.sort(key=lambda route: (-len(route.path), route.path, route.match))
    frozen_backends = MappingProxyType(dict(sorted(backends.items())))
    config = GatewayConfig(
        version=version,
        published_at=published_at,
        backends=frozen_backends,
        routes=tuple(routes),
        raw=dict(data),
    )
    return config


def read_config_file(
    path: str,
    *,
    catalog: Mapping[str, str] | None = None,
    reserved_prefixes: tuple[str, ...] = ("/__gateway",),
) -> GatewayConfig:
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        raise ConfigLoadError(f"无法读取配置文件 {path}：{exc}") from exc
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigLoadError(f"配置文件不是合法 JSON：{exc}") from exc
    try:
        return parse_config(
            data, catalog=catalog, reserved_prefixes=reserved_prefixes
        )
    except ConfigValidationError as exc:
        raise ConfigLoadError("配置文件校验失败：" + "；".join(exc.errors)) from exc


def config_file_signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    """Signature used to notice atomic replacements (inode changes on rename)."""
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mtime_ns,
        stat_result.st_size,
    )
