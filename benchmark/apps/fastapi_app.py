# pyright: reportMissingImports=false

import ujson
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

HELLO = "Hello, World!"
JSON_MEDIA_TYPE = "application/json"

app = FastAPI()


class UserInput(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0, le=150)


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
async def validate(body: UserInput) -> Response:
    return json_response({"name": body.name, "age": body.age, "valid": True})
