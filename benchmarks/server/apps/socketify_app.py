# pyright: reportMissingImports=false

import os
from io import BytesIO

from apps.common import validate_fields
from socketify import App, AppListenOptions

HELLO = "Hello, World!"
app = App()

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


def plaintext(res, req):
    res.end(HELLO)


def json_endpoint(res, req):
    res.end({"message": HELLO})


def get_user(res, req):
    user_id = req.get_parameter(0)
    res.end({"id": user_id, "name": f"User {user_id}"})


async def validate(res, req):
    body = await res.get_json() or {}
    payload, status = validate_fields(body)
    if status != 200:
        return res.write_status(status).end(payload)
    res.end(payload)


async def post_form(res, req):
    await _read_body(res)
    res.write_status(204).end_without_body()


async def post_echo_json(res, req):
    data = await _read_body(res)
    res.end({"bytes": len(data)})


async def ingest_buffer(res, req):
    data = await _read_body(res)
    res.end({"bytes": len(data)})


async def ingest_stream(res, req):
    data = await _read_body(res)
    res.end({"bytes": len(data)})


async def upload(res, req):
    data = await _read_body(res)
    res.end({"bytes": len(data)})


app.get("/plaintext", plaintext)
app.get("/json", json_endpoint)
app.get("/user/:user_id", get_user)
app.post("/validate", validate)
app.post("/form", post_form)
app.post("/echo/json", post_echo_json)
app.post("/ingest/64k", ingest_buffer)
app.post("/ingest/2m", ingest_buffer)
app.post("/ingest/stream/2m", ingest_stream)
app.post("/upload", upload)

host = os.environ.get("BENCH_HOST", "127.0.0.1")
port = int(os.environ.get("BENCH_PORT", "3000"))
app.listen(AppListenOptions(port=port, host=host), lambda _cfg: None)
app.run()
