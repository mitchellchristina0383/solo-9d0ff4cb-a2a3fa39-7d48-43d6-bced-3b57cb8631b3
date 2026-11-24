from __future__ import annotations

import json
import os
import threading
import time

from common.storage import atomic_write_json, read_version


def test_atomic_write_replaces_contents(tmp_path):
    path = tmp_path / "routes.json"
    atomic_write_json(str(path), {"version": 1})
    atomic_write_json(str(path), {"version": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 2}
    assert read_version(str(path)) == 2


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "routes.json"
    atomic_write_json(str(path), {"version": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["routes.json"]


def test_atomic_write_failure_keeps_original(tmp_path):
    path = tmp_path / "routes.json"
    atomic_write_json(str(path), {"version": 1})
    original = path.read_text(encoding="utf-8")
    bad = tmp_path / "missing-dir" / "routes.json"
    try:
        atomic_write_json(str(bad), {"version": 2})
    except Exception:  # noqa: BLE001
        pass
    assert path.read_text(encoding="utf-8") == original
    assert read_version(str(path)) == 1


def test_concurrent_readers_never_see_partial_file(tmp_path):
    path = tmp_path / "routes.json"
    atomic_write_json(str(path), {"version": 0, "routes": []})
    stop = threading.Event()
    failures: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                with open(path, encoding="utf-8") as handle:
                    json.load(handle)
            except Exception as exc:  # noqa: BLE001
                failures.append(repr(exc))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for version in range(1, 200):
            atomic_write_json(str(path), {"version": version, "routes": []})
    finally:
        stop.set()
        thread.join()
    assert failures == []


def test_write_is_mode_limited(tmp_path):
    path = tmp_path / "routes.json"
    atomic_write_json(str(path), {"version": 1})
    assert os.stat(path).st_mode & 0o777 == 0o640


def test_read_version_of_corrupt_file(tmp_path):
    path = tmp_path / "routes.json"
    path.write_text("nope", encoding="utf-8")
    assert read_version(str(path)) is None
