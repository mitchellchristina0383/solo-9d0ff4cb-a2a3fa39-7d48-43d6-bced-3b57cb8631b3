from __future__ import annotations

import json

import pytest

from common.config import (
    ConfigLoadError,
    ConfigValidationError,
    GatewayConfig,
    parse_config,
    read_config_file,
)

CATALOG = {
    "sample-a": "http://sample-a:9000",
    "sample-b": "http://sample-b:9000",
}


def base_config(**overrides):
    data = {
        "schema_version": 1,
        "version": 3,
        "published_at": "2026-01-01T00:00:00+00:00",
        "backends": [
            {"name": "sample-a", "url": "http://sample-a:9000"},
            {"name": "sample-b", "url": "http://sample-b:9000"},
        ],
        "routes": [
            {"path": "/a", "match": "prefix", "backend": "sample-a"},
            {"path": "/b", "match": "prefix", "backend": "sample-b"},
        ],
    }
    data.update(overrides)
    return data


def test_parse_valid_config():
    config = parse_config(base_config(), catalog=CATALOG)
    assert isinstance(config, GatewayConfig)
    assert config.version == 3
    assert set(config.backends) == {"sample-a", "sample-b"}
    assert len(config.routes) == 2


def test_empty_route_table_is_valid():
    config = parse_config(
        base_config(routes=[]),
        catalog=CATALOG,
    )
    assert config.routes == ()
    assert config.match("/a") is None


def test_route_to_unknown_backend_rejected():
    data = base_config(
        routes=[{"path": "/a", "match": "prefix", "backend": "nope"}]
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        parse_config(data, catalog=CATALOG)
    assert any("nope" in message for message in excinfo.value.errors)


def test_backend_outside_catalog_rejected():
    data = base_config(
        backends=[{"name": "elsewhere", "url": "http://elsewhere:9000"}],
        routes=[{"path": "/a", "backend": "elsewhere"}],
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        parse_config(data, catalog=CATALOG)
    assert any("不在 Compose 样例服务列表" in m for m in excinfo.value.errors)


def test_backend_url_must_match_catalog():
    data = base_config(
        backends=[{"name": "sample-a", "url": "http://evil:9000"}],
        routes=[{"path": "/a", "backend": "sample-a"}],
    )
    with pytest.raises(ConfigValidationError):
        parse_config(data, catalog=CATALOG)


def test_duplicate_route_path_rejected():
    data = base_config(
        routes=[
            {"path": "/a", "backend": "sample-a"},
            {"path": "/a", "backend": "sample-b"},
        ]
    )
    with pytest.raises(ConfigValidationError):
        parse_config(data, catalog=CATALOG)


def test_duplicate_backend_name_rejected():
    data = base_config(
        backends=[
            {"name": "sample-a", "url": "http://sample-a:9000"},
            {"name": "sample-a", "url": "http://sample-a:9000"},
        ]
    )
    with pytest.raises(ConfigValidationError):
        parse_config(data, catalog=CATALOG)


def test_reserved_prefix_rejected():
    with pytest.raises(ConfigValidationError):
        parse_config(
            base_config(routes=[{"path": "/__gateway/x", "backend": "sample-a"}]),
            catalog=CATALOG,
        )


@pytest.mark.parametrize(
    "path",
    ["", "relative", "/a/../b", "/a?x=1", "/a#frag", "/a b", 3, None],
)
def test_invalid_paths_rejected(path):
    with pytest.raises(ConfigValidationError):
        parse_config(
            base_config(routes=[{"path": path, "backend": "sample-a"}]),
            catalog=CATALOG,
        )


@pytest.mark.parametrize(
    "url",
    ["ftp://x", "http://", "http://user:pass@x:1", "http://x:99999", "http://x/?a=1"],
)
def test_invalid_backend_urls_rejected(url):
    data = base_config(
        backends=[{"name": "sample-a", "url": url}],
        routes=[{"path": "/a", "backend": "sample-a"}],
    )
    with pytest.raises(ConfigValidationError):
        parse_config(data, catalog=None)


def test_route_matching_prefers_longest_prefix():
    config = parse_config(
        base_config(
            routes=[
                {"path": "/", "match": "prefix", "backend": "sample-a"},
                {"path": "/api", "match": "prefix", "backend": "sample-b"},
                {"path": "/api/v2", "match": "prefix", "backend": "sample-a"},
                {"path": "/exact", "match": "exact", "backend": "sample-b"},
            ]
        ),
        catalog=CATALOG,
    )
    assert config.match("/anything").path == "/"
    assert config.match("/api").path == "/api"
    assert config.match("/api/users").path == "/api"
    assert config.match("/api/v2/x").path == "/api/v2"
    assert config.match("/exactly").path == "/"
    assert config.match("/exact").path == "/exact"
    assert config.match("/exact/sub").path == "/"


def test_prefix_does_not_match_sibling_name():
    config = parse_config(
        base_config(routes=[{"path": "/api", "backend": "sample-a"}]),
        catalog=CATALOG,
    )
    assert config.match("/apifoo") is None
    assert config.match("/api/foo") is not None


def test_read_config_file_reports_bad_json(tmp_path):
    path = tmp_path / "routes.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        read_config_file(str(path), catalog=CATALOG)


def test_read_config_file_missing(tmp_path):
    with pytest.raises(ConfigLoadError):
        read_config_file(str(tmp_path / "missing.json"), catalog=CATALOG)


def test_config_is_immutable():
    config = parse_config(base_config(), catalog=CATALOG)
    with pytest.raises(TypeError):
        config.backends["x"] = None  # type: ignore[index]
