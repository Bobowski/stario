from typing import Annotated

from starlette.testclient import TestClient

from stario import Query, Stario
from stario.parameters import Headers


def test_headers_ok_multiple_values():
    async def handler(values: Annotated[list[str], Headers("x-dup")]):
        return ",".join(values)

    app = Stario(Query("/hh", handler))

    with TestClient(app) as client:
        resp = client.get("/hh", headers=[("x-dup", "a"), ("x-dup", "b")])
    assert resp.status_code == 200
    assert resp.text == "a,b"


def test_headers_missing():
    async def handler(values: Annotated[list[str], Headers("x-dup")]):
        return ",".join(values)

    app = Stario(Query("/hh", handler))

    with TestClient(app) as client:
        resp = client.get("/hh")
    assert resp.status_code == 400
    assert "Missing required header 'x-dup'" in resp.text


def test_headers_type_validation():
    # Expect list[int], provide non-int among values
    async def handler(values: Annotated[list[int], Headers("x-nums")]):
        return ",".join(str(v) for v in values)

    app = Stario(Query("/hic", handler))

    with TestClient(app) as client:
        resp = client.get("/hic", headers=[("x-nums", "1"), ("x-nums", "x")])
    assert resp.status_code == 422
    assert "Invalid header 'x-nums'" in resp.text
