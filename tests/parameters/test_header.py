from typing import Annotated

from starlette.testclient import TestClient

from stario import Query, Stario
from stario.parameters import ParseHeader


def test_header_ok():
    async def handler(token: Annotated[str, ParseHeader("x-token")]):
        return token

    app = Stario(Query("/h", handler))

    with TestClient(app) as client:
        resp = client.get("/h", headers={"x-token": "abc"})
    assert resp.text == "abc"
    assert resp.status_code == 200


def test_header_missing():
    async def handler(token: Annotated[str, ParseHeader("x-token")]):
        return token

    app = Stario(Query("/h", handler))

    with TestClient(app) as client:
        resp = client.get("/h")
    assert resp.status_code == 400
    assert "Missing required header 'x-token'" in resp.text


def test_header_invalid_type():
    # Expect int, send non-int; header values are strings
    async def handler(x: Annotated[int, ParseHeader("x-num")]):
        return str(x)

    app = Stario(Query("/hn", handler))

    with TestClient(app) as client:
        resp = client.get("/hn", headers={"x-num": "not-int"})
    assert resp.status_code == 422
    assert "Invalid header 'x-num'" in resp.text
