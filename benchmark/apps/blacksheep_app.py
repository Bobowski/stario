# pyright: reportMissingImports=false

import ujson
from blacksheep import Application, Content, Request, Response
from blacksheep.server.responses import text as text_response

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
    body = ujson.loads(await request.read())
    name = body.get("name")
    age = body.get("age")

    if not isinstance(name, str) or not name:
        return json_response({"error": "name must be a non-empty string"}, 400)
    if not isinstance(age, int) or age < 0 or age > 150:
        return json_response(
            {"error": "age must be an integer between 0 and 150"},
            400,
        )

    return json_response({"name": name, "age": age, "valid": True})
