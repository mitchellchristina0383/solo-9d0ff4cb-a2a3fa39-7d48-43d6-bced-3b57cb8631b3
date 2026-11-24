"""Compose init service: prepare the shared volume and seed a first config.

Run as root by the ``config-init`` service.  It fixes ownership so the
non-root gateway (read only) and publisher (read/write) can use the volume,
then writes a demo configuration only when no configuration exists yet.
"""

from __future__ import annotations

import logging
import os
import sys

from common.config import (
    SCHEMA_VERSION,
    catalog_from_env,
    parse_config,
    utc_now_iso,
)
from common.storage import atomic_write_json, read_version

LOG = logging.getLogger("config-seed")


def build_seed(catalog: dict[str, str]) -> dict:
    backends = [{"name": name, "url": url} for name, url in sorted(catalog.items())]
    routes = [
        {"path": f"/{name}", "match": "prefix", "backend": name}
        for name in sorted(catalog)
    ]
    data = {
        "schema_version": SCHEMA_VERSION,
        "version": 1,
        "published_at": utc_now_iso(),
        "backends": backends,
        "routes": routes,
    }
    # Refuse to write anything the gateway would reject.
    parse_config(data, catalog=catalog)
    return data


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    path = os.environ.get("CONFIG_PATH", "/data/routes.json")
    directory = os.path.dirname(os.path.abspath(path))
    catalog = catalog_from_env(os.environ.get("BACKEND_CATALOG"))
    uid = int(os.environ.get("DATA_UID", "10001"))
    gid = int(os.environ.get("DATA_GID", "10001"))

    os.makedirs(directory, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(directory, uid, gid)
    os.chmod(directory, 0o770)

    if not os.path.exists(path):
        data = build_seed(catalog)
        atomic_write_json(path, data)
        LOG.info("seeded initial configuration at %s (version 1)", path)
    else:
        version = read_version(path)
        LOG.info("configuration already present at %s (version %s)", path, version)

    if os.geteuid() == 0:
        os.chown(path, uid, gid)
    os.chmod(path, 0o640)
    LOG.info("shared volume prepared: dir=%s uid=%s gid=%s", directory, uid, gid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
