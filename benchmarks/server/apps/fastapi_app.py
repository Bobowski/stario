# pyright: reportMissingImports=false

import ujson
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from apps.common import validate_fields

HELLO = "Hello, World!"
JSON_MEDIA_TYPE = "application/json"

app = FastAPI()


def json_response(value: object, status_code: int = 200) -> Response:
    return Response(
        ujson.dumps(value).encode("utf-8"),
        status_code=status_code,
        media_type=JSON_MEDIA_TYPE,
    )


@app.get("/plaintext")
async def plaintext() -> PlainTextResponse:
    return PlainTextResponse(HELLO)


@app.get("/json")
async def json_endpoint() -> Response:
    return json_response({"message": HELLO})


@app.get("/user/{user_id}")
async def get_user(user_id: str) -> Response:
    return json_response({"id": user_id, "name": f"User {user_id}"})


@app.post("/validate")
async def validate(request: Request) -> Response:
    payload, status = validate_fields(ujson.loads(await request.body()))
    return json_response(payload, status)


@app.post("/form")
async def post_form(request: Request) -> Response:
    await request.body()
    return Response(status_code=204)


@app.post("/echo/json")
async def post_echo_json(request: Request) -> Response:
    body = await request.body()
    return json_response({"bytes": len(body)})


@app.post("/ingest/64k")
@app.post("/ingest/2m")
async def ingest_buffer(request: Request) -> Response:
    body = await request.body()
    return json_response({"bytes": len(body)})


@app.post("/ingest/stream/2m")
async def ingest_stream(request: Request) -> Response:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    return json_response({"bytes": total})


@app.post("/upload")
async def upload(request: Request) -> Response:
    body = await request.body()
    return json_response({"bytes": len(body)})
