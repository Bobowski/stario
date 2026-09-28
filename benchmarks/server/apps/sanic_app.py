# pyright: reportMissingImports=false

import argparse

import ujson
from sanic import Sanic, raw
from sanic.views import stream

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

app = Sanic("stario_benchmark_sanic")
app.config.ACCESS_LOG = False
app.config.MOTD = False
app.config.RESPONSE_TIMEOUT = 120
app.config.REQUEST_TIMEOUT = 120
app.config.REQUEST_MAX_SIZE = 4 * 1024 * 1024


def text_response(line: str | bytes):
    body = line if isinstance(line, bytes) else line.encode("ascii")
    return raw(body, content_type=TEXT_CONTENT_TYPE_STR)


@app.get("/plaintext")
async def plaintext(request):
    return text_response(PLAINTEXT_BODY)


@app.get("/user/<user_id>")
async def read_request(request, user_id: str):
    return text_response(
        request_line(
            user_id,
            query_value(request.args.get("q")),
            as_str(request.headers.get(REQUEST_HEADER)),
        )
    )


@app.post("/echo")
async def post_json(request):
    raw_body = request.body
    await yield_once()
    return text_response(json_echo_line(ujson.loads(raw_body) if raw_body else {}))


@app.post("/ingest/64k", name="ingest_64k")
@app.post("/ingest/2m", name="ingest_2m")
async def ingest_buffer(request):
    body = request.body
    await yield_once()
    return text_response(bytes_line(len(body)))


@app.post("/ingest/stream/2m", name="ingest_stream_2m")
@stream
async def ingest_stream(request):
    total = 0
    async for chunk in request.stream:
        total += len(chunk)
    await yield_once()
    return text_response(bytes_line(total))


@app.post("/upload")
async def upload(request):
    body = request.body
    await yield_once()
    return text_response(bytes_line(len(body)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    app.run(
        host=args.host,
        port=args.port,
        single_process=True,
        access_log=False,
        debug=False,
        motd=False,
    )
