from typing import Annotated

from starlette.testclient import TestClient

from stario import Stario
from stario.requests import ParsePathParam
from stario.routes import Query


def test_path_param_ok():
    async def handler(id: Annotated[int, ParsePathParam()]):
        return str(id)

    app = Stario(Query("/items/{id}", handler))

    with TestClient(app) as client:
        resp = client.get("/items/5")
    assert resp.status_code == 200
    assert resp.text == "5"


def test_path_param_missing_not_match():
    async def handler(id: Annotated[int, ParsePathParam()]):
        return str(id)

    app = Stario(Query("/items/{id}", handler))

    with TestClient(app) as client:
        resp = client.get("/items/")
    # Starlette will 404 when path does not match
    assert resp.status_code == 404


def test_path_param_invalid_type():
    async def handler(id: Annotated[int, ParsePathParam()]):
        return str(id)

    app = Stario(Query("/items/{id}", handler))

    with TestClient(app) as client:
        resp = client.get("/items/abc")
    assert resp.status_code == 422
    assert "Invalid path parameter 'id'" in resp.text
