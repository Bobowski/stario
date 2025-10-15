from typing import Annotated

from starlette.testclient import TestClient

from stario import Command, Stario
from stario.requests import ParseBody


def test_body_ok_raw_bytes():
    async def handler(data: Annotated[bytes, ParseBody()]):
        # Return hex length for stable text
        return str(len(data))

    app = Stario(Command("/b", handler))

    with TestClient(app) as client:
        resp = client.post("/b", content=b"hello")
    assert resp.text == "5"
    assert resp.status_code == 200


def test_body_missing_is_empty():
    async def handler(data: Annotated[bytes, ParseBody()]):
        return str(len(data))

    app = Stario(Command("/b", handler))

    with TestClient(app) as client:
        resp = client.post("/b")
    # Empty body should be allowed; length 0
    assert resp.text == "0"
    assert resp.status_code == 200


def test_body_string_len_with_multibyte():
    async def handler(data: Annotated[str, ParseBody()]):
        # Return the length of the string (number of characters)
        return str(len(data))

    app = Stario(Command("/b", handler))

    with TestClient(app) as client:
        # Send a multibyte UTF-8 string: 4 bytes, but 1 character
        resp = client.post("/b", content="💩".encode("utf-8"))
    # The string is one Unicode character, even though it's 4 bytes
    assert len("💩".encode("utf-8")) == 4
    assert resp.text == "1"
    assert resp.status_code == 200


def test_body_invalid_type_expect_int():
    async def handler(num: Annotated[int, ParseBody()]):
        return str(num)

    app = Stario(Command("/n", handler))

    with TestClient(app) as client:
        # Provide JSON content-type, but invalid value for int
        resp = client.post(
            "/n",
            content=b'"not-int"',
            headers={"Content-Type": "application/json"},
        )
    assert "Invalid request body" in resp.text
    assert resp.status_code == 422


def test_body_json_to_dict_ok():
    async def handler(payload: Annotated[dict, ParseBody()]):
        # Ensure we actually received a dict
        if payload.get("a") == 1 and payload.get("b") == "x":
            return "ok"
        return "bad"

    app = Stario(Command("/j", handler))

    with TestClient(app) as client:
        resp = client.post("/j", json={"a": 1, "b": "x"})
    assert resp.text == "ok"
    assert resp.status_code == 200


def test_body_json_expected_dict_but_plain_text():
    async def handler(payload: Annotated[dict, ParseBody()]):
        return "never"

    app = Stario(Command("/j2", handler))

    with TestClient(app) as client:
        resp = client.post("/j2", content=b"just text")
    # Missing application/json header -> unsupported media type
    assert resp.status_code == 415
    assert "Unsupported media type" in resp.text


def test_body_json_expected_dict_empty_body():
    async def handler(payload: Annotated[dict, ParseBody()]):
        return "never"

    app = Stario(Command("/j3", handler))

    with TestClient(app) as client:
        resp = client.post("/j3", headers={"Content-Type": "application/json"})
    assert "Invalid request body" in resp.text
    assert resp.status_code == 422
