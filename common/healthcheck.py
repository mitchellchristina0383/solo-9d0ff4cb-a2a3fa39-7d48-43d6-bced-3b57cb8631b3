"""Tiny dependency-free HTTP health check used by Compose healthchecks.

Usage: python -m common.healthcheck http://127.0.0.1:8000/__gateway/healthz
Exits 0 when the endpoint answers with a 2xx/3xx status, 1 otherwise.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

TIMEOUT = 2.0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m common.healthcheck URL", file=sys.stderr)
        return 2
    try:
        with urllib.request.urlopen(argv[1], timeout=TIMEOUT) as response:
            return 0 if 200 <= response.status < 400 else 1
    except (urllib.error.URLError, OSError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
