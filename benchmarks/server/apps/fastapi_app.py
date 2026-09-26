# pyright: reportMissingImports=false

import ujson
from fastapi import FastAPI, Request, Response
from starlette.routing import Route

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

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def text_response(line: str | bytes) -> Response:
    body = line if isinstance(line, bytes) else line.encode("ascii")
    return Response(body, media_type=TEXT_CONTENT_TYPE_STR)


async def plaintext(_request: Request) -> Response:
    return text_response(PLAINTEXT_BODY)


async def read_request(request: Request) -> Response:
    return text_response(
        request_line(
            request.path_params["user_id"],
            query_value(request.query_params.get("q")),
            as_str(request.headers.get(REQUEST_HEADER)),
        )
    )


async def post_json(request: Request) -> Response:
    raw = await request.body()
    await yield_once()
    return text_response(json_echo_line(ujson.loads(raw) if raw else {}))


async def ingest_buffer(request: Request) -> Response:
    body = await request.body()
    await yield_once()
    return text_response(bytes_line(len(body)))


async def ingest_stream(request: Request) -> Response:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    await yield_once()
    return text_response(bytes_line(total))


async def upload(request: Request) -> Response:
    body = await request.body()
    await yield_once()
    return text_response(bytes_line(len(body)))


app.router.routes = [
    Route("/plaintext", plaintext, methods=["GET"]),
    Route("/user/{user_id}", read_request, methods=["GET"]),
    Route("/echo", post_json, methods=["POST"]),
    Route("/ingest/64k", ingest_buffer, methods=["POST"]),
    Route("/ingest/2m", ingest_buffer, methods=["POST"]),
    Route("/ingest/stream/2m", ingest_stream, methods=["POST"]),
    Route("/upload", upload, methods=["POST"]),
]
