# pyright: reportMissingImports=false

import ujson
from blacksheep import Application, Request, Response
from blacksheep.server.responses import json as bs_json
from blacksheep.server.responses import no_content, text as text_response
from blacksheep.server.routing import Router
from blacksheep.settings.json import json_settings

from apps.common import validate_fields

HELLO = "Hello, World!"
JSON_CONTENT_TYPE = b"application/json"

json_settings.use(loads=ujson.loads, dumps=ujson.dumps)

app = Application(router=Router(), show_error_details=False)


@app.router.get("/plaintext")
async def plaintext(request: Request) -> Response:
    return text_response(HELLO)


@app.router.get("/json")
async def json_endpoint(request: Request) -> Response:
    return bs_json({"message": HELLO})


@app.router.get("/user/{user_id}")
async def get_user(request: Request) -> Response:
    user_id = request.route_values["user_id"]
    return bs_json({"id": user_id, "name": f"User {user_id}"})


@app.router.post("/validate")
async def validate(request: Request) -> Response:
    payload, status = validate_fields(ujson.loads(await request.read()))
    return bs_json(payload, status)


@app.router.post("/form")
async def post_form(request: Request) -> Response:
    await request.read()
    return no_content()


@app.router.post("/echo/json")
async def post_echo_json(request: Request) -> Response:
    body = await request.read()
    return bs_json({"bytes": len(body)})


@app.router.post("/ingest/64k")
@app.router.post("/ingest/2m")
async def ingest_buffer(request: Request) -> Response:
    body = await request.read()
    return bs_json({"bytes": len(body)})


@app.router.post("/ingest/stream/2m")
async def ingest_stream(request: Request) -> Response:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    return bs_json({"bytes": total})


@app.router.post("/upload")
async def upload(request: Request) -> Response:
    body = await request.read()
    return bs_json({"bytes": len(body)})
