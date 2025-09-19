from typing import Annotated

from starlette.testclient import TestClient

from stario import Command, Stario
from stario.parameters import ParseJsonBody


def test_json_body_dict_ok():
    async def handler(payload: Annotated[dict, ParseJsonBody[dict]()]):
        return payload.get("k", "missing")

    app = Stario(Command("/json", handler))

    with TestClient(app) as client:
        resp = client.post("/json", json={"k": "v"})
    assert resp.status_code == 200
    assert resp.text == "v"


def test_json_body_list_ok():
    async def handler(items: Annotated[list[int], ParseJsonBody[list[int]]()]):
        return str(sum(items))

    app = Stario(Command("/json-list", handler))

    with TestClient(app) as client:
        resp = client.post("/json-list", json=[1, 2, 3])
    assert resp.status_code == 200
    assert resp.text == "6"


def test_json_body_invalid_payload():
    async def handler(payload: Annotated[dict, ParseJsonBody[dict]()]):
        return "never"

    app = Stario(Command("/json-bad", handler))

    with TestClient(app) as client:
        resp = client.post(
            "/json-bad",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 422
    assert "Invalid request body" in resp.text
