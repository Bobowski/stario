from typing import Annotated

from starlette.testclient import TestClient

from stario import Query, Stario
from stario.parameters import QueryParam


def test_query_param_ok():
    async def handler(q: Annotated[int, QueryParam()]):
        return str(q)

    app = Stario(Query("/q", handler))

    with TestClient(app) as client:
        resp = client.get("/q", params={"q": 42})
    assert resp.status_code == 200
    assert resp.text == "42"


def test_query_param_missing():
    async def handler(q: Annotated[int, QueryParam()]):
        return str(q)

    app = Stario(Query("/q", handler))

    with TestClient(app) as client:
        resp = client.get("/q")
    assert resp.status_code == 400
    assert "Missing required query parameter 'q'" in resp.text


def test_query_param_invalid_type():
    async def handler(q: Annotated[int, QueryParam()]):
        return str(q)

    app = Stario(Query("/q", handler))

    with TestClient(app) as client:
        resp = client.get("/q", params={"q": "not-an-int"})
    assert resp.status_code == 422
    assert "Invalid query parameter 'q'" in resp.text


def test_query_param_with_default():
    async def handler(q: Annotated[int, QueryParam()] = 7):
        return str(q)

    app = Stario(Query("/q", handler))

    with TestClient(app) as client:
        resp = client.get("/q")
    assert resp.status_code == 200
    assert resp.text == "7"
