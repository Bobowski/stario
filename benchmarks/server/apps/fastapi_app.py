# pyright: reportMissingImports=false

import ujson
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.routing import Route

from apps.common import validate_fields

HELLO = "Hello, World!"
JSON_MEDIA_TYPE = "application/json"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def json_response(value: object, status_code: int = 200) -> Response:
    return Response(
        ujson.dumps(value).encode("utf-8"),
        status_code=status_code,
        media_type=JSON_MEDIA_TYPE,
    )


async def plaintext(_request: Request) -> PlainTextResponse:
    return PlainTextResponse(HELLO)


async def json_endpoint(_request: Request) -> Response:
    return json_response({"message": HELLO})


async def get_user(request: Request) -> Response:
    user_id = request.path_params["user_id"]
    return json_response({"id": user_id, "name": f"User {user_id}"})


async def validate(request: Request) -> Response:
    payload, status = validate_fields(ujson.loads(await request.body()))
    return json_response(payload, status)


async def post_form(request: Request) -> Response:
    await request.body()
    return Response(status_code=204)


async def post_echo_json(request: Request) -> Response:
    body = await request.body()
    return json_response({"bytes": len(body)})


async def ingest_buffer(request: Request) -> Response:
    body = await request.body()
    return json_response({"bytes": len(body)})


async def ingest_stream(request: Request) -> Response:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    return json_response({"bytes": total})


async def upload(request: Request) -> Response:
    body = await request.body()
    return json_response({"bytes": len(body)})


app.router.routes = [
    Route("/plaintext", plaintext, methods=["GET"]),
    Route("/json", json_endpoint, methods=["GET"]),
    Route("/user/{user_id}", get_user, methods=["GET"]),
    Route("/validate", validate, methods=["POST"]),
    Route("/form", post_form, methods=["POST"]),
    Route("/echo/json", post_echo_json, methods=["POST"]),
    Route("/ingest/64k", ingest_buffer, methods=["POST"]),
    Route("/ingest/2m", ingest_buffer, methods=["POST"]),
    Route("/ingest/stream/2m", ingest_stream, methods=["POST"]),
    Route("/upload", upload, methods=["POST"]),
]
