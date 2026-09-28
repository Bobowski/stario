# pyright: reportMissingImports=false

import os

import ujson
from robyn import Config, Robyn
from robyn.robyn import Headers, Response

from apps.common import (
    PLAINTEXT_BODY,
    REQUEST_HEADER,
    TEXT_CONTENT_TYPE_STR,
    as_str,
    bytes_line,
    json_echo_line,
    query_value,
    request_line,
    yield_once,
)

config = Config()
app = Robyn(__file__, config=config)


def _body_bytes(request) -> bytes:
    body = request.body
    if isinstance(body, str):
        return body.encode("utf-8")
    return body or b""


def _query_map(request) -> object:
    return getattr(request, "query_params", None) or getattr(request, "queries", {})


def _header(request, name: str) -> str:
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    for key in (name, name.title(), name.upper()):
        try:
            value = getter(key)
        except TypeError:
            value = getter(key, None)
        if value:
            return as_str(value)
    return ""


def text_response(line: str | bytes) -> Response:
    body = line if isinstance(line, bytes) else line.encode("ascii")
    return Response(
        status_code=200,
        headers=Headers({"Content-Type": TEXT_CONTENT_TYPE_STR}),
        description=body,
    )


@app.get("/plaintext")
async def plaintext():
    return text_response(PLAINTEXT_BODY)


@app.get("/user/:user_id")
async def read_request(request):
    params = _query_map(request)
    getter = getattr(params, "get", None)
    q = query_value(getter("q", "") if getter else None)
    return request_line(
        request.path_params["user_id"],
        q,
        _header(request, REQUEST_HEADER),
    )


@app.post("/echo")
async def post_json(request):
    raw = _body_bytes(request)
    await yield_once()
    return json_echo_line(ujson.loads(raw) if raw else {})


@app.post("/ingest/64k")
@app.post("/ingest/2m")
async def ingest_buffer(request):
    body = _body_bytes(request)
    await yield_once()
    return bytes_line(len(body))


@app.post("/ingest/stream/2m")
async def ingest_stream(request):
    body = _body_bytes(request)
    await yield_once()
    return bytes_line(len(body))


@app.post("/upload")
async def upload(request):
    body = _body_bytes(request)
    await yield_once()
    return bytes_line(len(body))


if __name__ == "__main__":
    host = os.environ.get("BENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCH_PORT", "8080"))
    app.start(host=host, port=port, _check_port=False)
