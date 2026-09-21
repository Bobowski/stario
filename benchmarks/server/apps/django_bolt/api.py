from typing import Annotated

import ujson
from django_bolt import BoltAPI
from django_bolt.param_functions import Header
from django_bolt.responses import PlainText

from apps.common import (
    HELLO,
    REQUEST_HEADER,
    bytes_line,
    json_echo_line,
    request_line,
    yield_once,
)

api = BoltAPI(validate_response=False)


@api.get("/plaintext")
async def plaintext():
    return PlainText(HELLO)


@api.get("/user/{user_id}")
async def read_request(
    user_id: str,
    q: str = "",
    x_request_id: Annotated[str, Header(alias=REQUEST_HEADER)] = "",
):
    return PlainText(request_line(user_id, q, x_request_id))


@api.post("/echo")
async def post_json(request):
    raw = request.body
    await yield_once()
    return PlainText(json_echo_line(ujson.loads(raw) if raw else {}))


@api.post("/ingest/64k")
@api.post("/ingest/2m")
async def ingest_buffer(request):
    body = request.body
    await yield_once()
    return PlainText(bytes_line(len(body)))


@api.post("/ingest/stream/2m")
async def ingest_stream(request):
    body = request.body
    await yield_once()
    return PlainText(bytes_line(len(body)))


@api.post("/upload")
async def upload(request):
    body = request.body
    await yield_once()
    return PlainText(bytes_line(len(body)))
