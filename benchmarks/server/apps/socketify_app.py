# pyright: reportMissingImports=false

import os
from io import BytesIO

import ujson

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
from socketify import App, AppListenOptions

app = App()
app.json_serializer(ujson)

# Pin response wrappers while C callbacks are in flight (Py3.14 + socketify ffi GC bug).
_INFLIGHT: dict[int, object] = {}


def _pin(res):
    _INFLIGHT[id(res)] = res
    return res


def _unpin(res):
    _INFLIGHT.pop(id(res), None)


async def _read_body(res) -> bytes:
    res = _pin(res)
    done = res.app.loop.create_future()
    buffer = BytesIO()

    def on_chunk(_response, chunk, is_end):
        if chunk is not None:
            buffer.write(chunk)
        if is_end and not done.done():
            done.set_result(buffer.getvalue())

    def on_aborted(_response):
        if not done.done():
            done.set_result(b"")

    try:
        res.on_aborted(on_aborted)
        res.on_data(on_chunk)
        return await done
    finally:
        _unpin(res)


def _text(res, line: str | bytes):
    body = line if isinstance(line, bytes) else line.encode("ascii")
    res.write_header("Content-Type", TEXT_CONTENT_TYPE_STR)
    res.end(body)


def plaintext(res, req):
    _text(res, PLAINTEXT_BODY)


def read_request(res, req):
    _text(
        res,
        request_line(
            req.get_parameter(0),
            query_value(req.get_query("q")),
            as_str(req.get_header(REQUEST_HEADER)),
        ),
    )


async def post_json(res, req):
    raw = await _read_body(res)
    await yield_once()
    _text(res, json_echo_line(ujson.loads(raw) if raw else {}))


async def ingest_buffer(res, req):
    data = await _read_body(res)
    await yield_once()
    _text(res, bytes_line(len(data)))


async def _read_stream(res) -> int:
    res = _pin(res)
    done = res.app.loop.create_future()
    total = 0

    def on_chunk(_response, chunk, is_end):
        nonlocal total
        if chunk is not None:
            total += len(chunk)
        if is_end and not done.done():
            done.set_result(total)

    def on_aborted(_response):
        if not done.done():
            done.set_result(0)

    try:
        res.on_aborted(on_aborted)
        res.on_data(on_chunk)
        return await done
    finally:
        _unpin(res)


async def ingest_stream(res, req):
    total = await _read_stream(res)
    await yield_once()
    _text(res, bytes_line(total))


async def upload(res, req):
    data = await _read_body(res)
    await yield_once()
    _text(res, bytes_line(len(data)))


app.get("/plaintext", plaintext)
app.get("/user/:user_id", read_request)
app.post("/echo", post_json)
app.post("/ingest/64k", ingest_buffer)
app.post("/ingest/2m", ingest_buffer)
app.post("/ingest/stream/2m", ingest_stream)
app.post("/upload", upload)

host = os.environ.get("BENCH_HOST", "127.0.0.1")
port = int(os.environ.get("BENCH_PORT", "3000"))
app.listen(AppListenOptions(port=port, host=host), lambda _cfg: None)
app.run()
