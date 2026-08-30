# pyright: reportMissingImports=false

import ujson

import stario.responses as responses
from apps.common import JSON_CONTENT_TYPE, validate_fields
from stario import App, Span

HELLO = "Hello, World!"


def json_response(w, value, status: int = 200) -> None:
    w.respond(ujson.dumps(value).encode("utf-8"), JSON_CONTENT_TYPE, status)


async def plaintext(c, w):
    responses.text(w, HELLO)


async def json_endpoint(c, w):
    json_response(w, {"message": HELLO})


async def get_user(c, w):
    user_id = c.route.params["user_id"]
    json_response(w, {"id": user_id, "name": f"User {user_id}"})


async def validate(c, w):
    body = ujson.loads(await c.req.body())
    payload, status = validate_fields(body)
    json_response(w, payload, status)


async def post_form(c, w):
    await c.req.body()
    responses.empty(w)


async def post_echo_json(c, w):
    body = await c.req.body()
    json_response(w, {"bytes": len(body)})


async def ingest_buffer(c, w):
    body = await c.req.body()
    json_response(w, {"bytes": len(body)})


async def ingest_stream(c, w):
    total = 0
    async for chunk in c.req.stream():
        total += len(chunk)
    json_response(w, {"bytes": total})


async def upload(c, w):
    body = await c.req.body()
    json_response(w, {"bytes": len(body)})


async def bootstrap(app: App, span: Span) -> None:
    app.get("/plaintext", plaintext)
    app.get("/json", json_endpoint)
    app.get("/user/{user_id}", get_user)
    app.post("/validate", validate)
    app.post("/form", post_form)
    app.post("/echo/json", post_echo_json)
    app.post("/ingest/64k", ingest_buffer)
    app.post("/ingest/2m", ingest_buffer)
    app.post("/ingest/stream/2m", ingest_stream)
    app.post("/upload", upload)
    yield
