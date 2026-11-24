"""Per-process, per-request configuration snapshots for the gateway."""

from __future__ import annotations

import logging
import os
import threading
from typing import Mapping

from common.config import (
    ConfigLoadError,
    GatewayConfig,
    config_file_signature,
    read_config_file,
)

LOG = logging.getLogger("gateway.config")


class ConfigStore:
    """Holds the last known-good configuration and reloads on file changes.

    ``snapshot()`` is called once at the start of every request.  The returned
    object is immutable, so an in-flight request is unaffected by later
    publishes.  Reload failures (missing file, invalid JSON, invalid routes)
    are logged and ignored; the previously active configuration stays in use.
    """

    def __init__(
        self,
        path: str,
        catalog: Mapping[str, str],
        reserved_prefixes: tuple[str, ...] = ("/__gateway",),
    ) -> None:
        self.path = path
        self.catalog = dict(catalog)
        self.reserved_prefixes = reserved_prefixes
        self._lock = threading.Lock()
        self._config: GatewayConfig | None = None
        self._signature: tuple[int, int, int, int] | None = None
        self._file_error: str | None = None
        self._last_logged_error: str | None = None

    def snapshot(self) -> GatewayConfig | None:
        try:
            stat_result = os.stat(self.path)
        except OSError as exc:
            self._handle_error(f"无法读取配置文件 {self.path}：{exc}")
            return self._config

        signature = config_file_signature(stat_result)
        with self._lock:
            if self._config is not None and signature == self._signature:
                return self._config
            try:
                config = read_config_file(
                    self.path,
                    catalog=self.catalog,
                    reserved_prefixes=self.reserved_prefixes,
                )
            except ConfigLoadError as exc:
                # Remember the bad signature so the same broken file is not
                # re-parsed on every request, but keep serving the old config.
                self._signature = signature
                self._file_error = str(exc)
                self._log_error_locked(str(exc))
                return self._config
            self._config = config
            self._signature = signature
            self._file_error = None
            self._last_logged_error = None
            LOG.info(
                "loaded configuration version=%s routes=%d backends=%d from %s",
                config.version,
                len(config.routes),
                len(config.backends),
                self.path,
            )
            return config

    def _handle_error(self, message: str) -> None:
        with self._lock:
            self._file_error = message
            self._log_error_locked(message)

    def _log_error_locked(self, message: str) -> None:
        # Caller holds self._lock.
        if message != self._last_logged_error:
            self._last_logged_error = message
            LOG.error(
                "configuration reload rejected, keeping active config "
                "(version=%s): %s",
                self._config.version if self._config else None,
                message,
            )

    def describe(self) -> dict:
        with self._lock:
            config = self._config
            return {
                "config_path": self.path,
                "active_version": config.version if config else None,
                "published_at": config.published_at if config else None,
                "route_count": len(config.routes) if config else 0,
                "config_file_error": self._file_error,
            }
