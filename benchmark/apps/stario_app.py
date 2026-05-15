# pyright: reportMissingImports=false

import ujson

import stario.responses as responses
from stario import App, Span

HELLO = "Hello, World!"
JSON_CONTENT_TYPE = b"application/json"


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
    name = body.get("name")
    age = body.get("age")

    if not isinstance(name, str) or not name:
        json_response(w, {"error": "name must be a non-empty string"}, 400)
        return
    if not isinstance(age, int) or age < 0 or age > 150:
        json_response(w, {"error": "age must be an integer between 0 and 150"}, 400)
        return

    json_response(w, {"name": name, "age": age, "valid": True})


async def bootstrap(app: App, span: Span) -> None:
    app.get("/plaintext", plaintext)
    app.get("/json", json_endpoint)
    app.get("/user/{user_id}", get_user)
    app.post("/validate", validate)
