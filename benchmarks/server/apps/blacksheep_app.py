# pyright: reportMissingImports=false

import ujson
from blacksheep import Application, Content, Request, Response
from blacksheep.server.routing import Router
from blacksheep.settings.json import json_settings

from apps.common import (
    PLAINTEXT_BODY,
    REQUEST_HEADER,
    TEXT_CONTENT_TYPE,
    as_str,
    bytes_line,
    json_echo_line,
    query_value,
    request_line,
    yield_once,
)

json_settings.use(loads=ujson.loads, dumps=ujson.dumps)

app = Application(router=Router(), show_error_details=False)


def text_response(line: str | bytes) -> Response:
    body = line if isinstance(line, bytes) else line.encode("ascii")
    return Response(200, content=Content(TEXT_CONTENT_TYPE, body))


@app.router.get("/plaintext")
async def plaintext(request: Request) -> Response:
    return text_response(PLAINTEXT_BODY)


@app.router.get("/user/{user_id}")
async def read_request(request: Request) -> Response:
    header = request.get_first_header(REQUEST_HEADER.encode("ascii"))
    return text_response(
        request_line(
            request.route_values["user_id"],
            query_value(request.query.get("q")),
            as_str(header),
        )
    )


@app.router.post("/echo")
async def post_json(request: Request) -> Response:
    raw = await request.read()
    await yield_once()
    return text_response(json_echo_line(ujson.loads(raw) if raw else {}))


@app.router.post("/ingest/64k")
@app.router.post("/ingest/2m")
async def ingest_buffer(request: Request) -> Response:
    body = await request.read()
    await yield_once()
    return text_response(bytes_line(len(body)))


@app.router.post("/ingest/stream/2m")
async def ingest_stream(request: Request) -> Response:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    await yield_once()
    return text_response(bytes_line(total))


@app.router.post("/upload")
async def upload(request: Request) -> Response:
    body = await request.read()
    await yield_once()
    return text_response(bytes_line(len(body)))
