from typing import Annotated, List

from starlette.testclient import TestClient

from stario import Query, Stario
from stario.requests import ParseQueryParams


def test_query_params_ok():

    async def handler(tags: Annotated[list[str], ParseQueryParams()]):
        return ",".join(tags)

    app = Stario(Query("/tags", handler))

    with TestClient(app) as client:
        resp = client.get("/tags", params=[("tags", "a"), ("tags", "b")])
    assert resp.status_code == 200
    assert resp.text == "a,b"


def test_query_params_missing():
    async def handler(tags: Annotated[List[str], ParseQueryParams()]):
        value = ",".join(tags)
        if value == "":
            return "empty"
        return value

    app = Stario(Query("/tags", handler))

    with TestClient(app) as client:
        resp = client.get("/tags")
    assert resp.text == "empty"
    assert resp.status_code == 200


def test_query_params_invalid_type():
    # Expect list[int], send strings
    async def handler(ids: Annotated[list[int], ParseQueryParams()]):
        return ",".join(str(i) for i in ids)

    app = Stario(Query("/ids", handler))

    with TestClient(app) as client:
        resp = client.get("/ids", params=[("ids", "a"), ("ids", "2")])
    assert resp.status_code == 422
    assert "Invalid query parameter 'ids'" in resp.text
