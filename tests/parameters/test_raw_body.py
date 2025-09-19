from typing import Annotated

from starlette.testclient import TestClient

from stario import Command, Stario
from stario.parameters import RawBody


def test_raw_body_bytes_ok():
    async def handler(data: Annotated[bytes, RawBody()]) -> str:
        return str(len(data))

    app = Stario(Command("/raw", handler))

    with TestClient(app) as client:
        resp = client.post("/raw", content=b"hello")
    assert resp.status_code == 200
    assert resp.text == "5"


def test_raw_body_str_ok_with_custom_encoding():
    async def handler(data: Annotated[str, RawBody(encoding="utf-8")]):
        return str(len(data))

    app = Stario(Command("/raw-str", handler))

    with TestClient(app) as client:
        resp = client.post("/raw-str", content="Żółć".encode("utf-8"))
    assert resp.status_code == 200
    assert resp.text == str(len("Żółć"))
