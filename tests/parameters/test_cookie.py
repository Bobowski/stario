from typing import Annotated

from starlette.testclient import TestClient

from stario import Stario
from stario.requests import ParseCookie
from stario.routes import Query


def test_cookie_ok():
    async def handler(session: Annotated[str, ParseCookie("session")]):
        return session

    app = Stario(Query("/c", handler))

    with TestClient(app) as client:
        resp = client.get("/c", headers={"cookie": "session=abc; theme=light"})
    assert resp.status_code == 200
    assert resp.text == "abc"


def test_cookie_missing():
    async def handler(session: Annotated[str, ParseCookie("session")]):
        return session

    app = Stario(Query("/c", handler))

    with TestClient(app) as client:
        resp = client.get("/c")
    assert resp.status_code == 400
    assert "Missing required cookie 'session'" in resp.text


def test_cookie_invalid_type():
    # Expect int from cookie (unusual but supported), provide string
    async def handler(num: Annotated[int, ParseCookie("num")]):
        return str(num)

    app = Stario(Query("/n", handler))

    with TestClient(app) as client:
        resp = client.get("/n", headers={"cookie": "num=notint"})
    assert resp.status_code == 422
    assert "Invalid cookie 'num'" in resp.text
