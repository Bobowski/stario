# pyright: reportMissingImports=false

import ujson
from blacksheep import Application, Content, Request, Response
from blacksheep.server.responses import text as text_response

from apps.common import validate_fields

HELLO = "Hello, World!"
JSON_CONTENT_TYPE = b"application/json"

app = Application(show_error_details=False)


def json_response(value: object, status: int = 200) -> Response:
    return Response(
        status,
        content=Content(JSON_CONTENT_TYPE, ujson.dumps(value).encode("utf-8")),
    )


@app.router.get("/plaintext")
async def plaintext():
    return text_response(HELLO)


@app.router.get("/json")
async def json_endpoint() -> Response:
    return json_response({"message": HELLO})


@app.router.get("/user/{user_id}")
async def get_user(user_id: str) -> Response:
    return json_response({"id": user_id, "name": f"User {user_id}"})


@app.router.post("/validate")
async def validate(request: Request) -> Response:
    payload, status = validate_fields(ujson.loads(await request.read()))
    return json_response(payload, status)


@app.router.post("/form")
async def post_form(request: Request) -> Response:
    await request.read()
    return Response(204)


@app.router.post("/echo/json")
async def post_echo_json(request: Request) -> Response:
    body = await request.read()
    return json_response({"bytes": len(body)})


@app.router.post("/ingest/64k")
@app.router.post("/ingest/2m")
async def ingest_buffer(request: Request) -> Response:
    body = await request.read()
    return json_response({"bytes": len(body)})


@app.router.post("/ingest/stream/2m")
async def ingest_stream(request: Request) -> Response:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    return json_response({"bytes": total})


@app.router.post("/upload")
async def upload(request: Request) -> Response:
    body = await request.read()
    return json_response({"bytes": len(body)})
