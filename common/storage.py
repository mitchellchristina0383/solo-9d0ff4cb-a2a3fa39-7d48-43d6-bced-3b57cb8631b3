"""Atomic on-disk storage for the shared routing configuration.

The publisher is the only writer.  It validates in memory and then replaces
the configuration file in a single ``os.replace`` so a concurrent reader (the
gateway) can only ever observe the complete old file or the complete new file,
never a partially written one.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

CONFIG_MODE = 0o640
TEMP_PREFIX = ".routes-"
TEMP_SUFFIX = ".tmp"


class ConfigWriteError(RuntimeError):
    """Raised when the new configuration could not be committed to disk."""


def atomic_write_json(path: str, data: Any, *, mode: int = CONFIG_MODE) -> int:
    """Write *data* as JSON and atomically replace *path*.

    Returns the number of bytes written.  On any failure the original file is
    left untouched and temporary files are removed.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd = -1
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1  # ownership transferred to the file object
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        tmp_path = None
        _fsync_directory(directory)
        try:
            return os.stat(path).st_size
        except OSError:
            return 0
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigWriteError(f"写入配置文件失败：{exc}") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _fsync_directory(directory: str) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def read_raw_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_version(path: str) -> int | None:
    """Best-effort version read used only to pick the next publish number."""
    try:
        data = read_raw_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        version = data.get("version")
        if isinstance(version, int) and not isinstance(version, bool) and version > 0:
            return version
    return None
