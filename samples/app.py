"""Sample Compose backend: identifies itself and can delay its response."""

from __future__ import annotations

import json
import logging
import os
import time

from flask import Flask, Response, request

app = Flask(__name__)
SERVICE_NAME = os.environ.get("SERVICE_NAME", "sample-a")
SERVICE_COLOR = os.environ.get("SERVICE_COLOR", "blue")
MAX_DELAY_MS = 120_000
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


@app.route("/", methods=ALL_METHODS)
@app.route("/<path:path>", methods=ALL_METHODS)
def echo(path: str = "") -> Response:
    try:
        delay_ms = int(request.args.get("delay_ms", "0"))
    except ValueError:
        delay_ms = 0
    delay_ms = max(0, min(delay_ms, MAX_DELAY_MS))
    started = time.monotonic()
    if delay_ms:
        time.sleep(delay_ms / 1000.0)
    waited_ms = int((time.monotonic() - started) * 1000)
    payload = {
        "service": SERVICE_NAME,
        "color": SERVICE_COLOR,
        "method": request.method,
        "path": "/" + path,
        "query": request.args.to_dict(flat=False),
        "requested_delay_ms": delay_ms,
        "waited_ms": waited_ms,
        "host": request.host,
        "headers": {
            key: value
            for key, value in request.headers.items()
            if key.lower().startswith("x-")
        },
        "body": request.get_data(as_text=True)[:2000],
    }
    response = Response(
        json.dumps(payload, ensure_ascii=False) + "\n",
        status=200,
        mimetype="application/json",
    )
    response.headers["X-Sample-Service"] = SERVICE_NAME
    response.headers["X-Sample-Color"] = SERVICE_COLOR
    return response


@app.get("/healthz")
def healthz() -> Response:
    return Response(
        json.dumps({"status": "ok", "service": SERVICE_NAME}),
        mimetype="application/json",
    )


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "9000")))
