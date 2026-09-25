"""Lock in server-benchmark helpers, wrk working sets, and Stario route shapes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from apps.common import (  # noqa: E402
    PARAM_ID_COUNT,
    PLAINTEXT_BODY,
    REQUEST_HEADER,
    bytes_line,
    json_echo_line,
    query_param,
    query_value,
    request_line,
)

SCRIPTS = SERVER_DIR / "scripts"


def test_param_working_set_exceeds_router_lru() -> None:
    assert PARAM_ID_COUNT > 1024


def test_lua_varies_path_query_and_header() -> None:
    lua = (SCRIPTS / "get-user.lua").read_text()
    assert f"local n = {PARAM_ID_COUNT}" in lua
    assert 'wrk.headers["X-Request-Id"]' in lua
    assert "?q=term" in lua
    assert "/user/" in lua


def test_request_line_includes_path_query_and_header() -> None:
    line = request_line("99", "term99", "h99")
    assert line == "user=99 q=term99 x=h99"


def test_query_param_and_header_helpers() -> None:
    assert query_param("q=term12&other=1", "q") == "term12"
    assert query_param(b"q=hello", "q") == "hello"
    assert query_param(None) == ""
    assert query_value(["a", "b"]) == "a"
    assert query_value(None) == ""


def test_upload_lines_are_plain_text() -> None:
    assert json_echo_line({"name": "Ada", "age": 42}) == "name=Ada age=42"
    assert bytes_line(1024) == "bytes=1024"


@pytest.mark.asyncio
async def test_stario_benchmark_app_route_shapes() -> None:
    pytest.importorskip("ujson")
    from apps.stario_app import bootstrap

    from stario.testing import TestClient

    async with TestClient(bootstrap) as client:
        plain = await client.get("/plaintext")
        assert plain.status_code == 200
        assert plain.content == PLAINTEXT_BODY

        first = await client.get(
            "/user/99",
            params={"q": "term99"},
            headers={REQUEST_HEADER: "h99"},
        )
        assert first.text == "user=99 q=term99 x=h99"

        second = await client.get(
            "/user/7",
            params={"q": "term7"},
            headers={REQUEST_HEADER: "h7"},
        )
        assert second.text == "user=7 q=term7 x=h7"

        echoed = await client.post("/echo", json={"name": "Ada", "age": 42})
        assert echoed.status_code == 200
        assert echoed.text == "name=Ada age=42"

        ingest = await client.post(
            "/ingest/64k",
            content=b"x" * 64,
            headers={"content-type": "application/octet-stream"},
        )
        assert ingest.text == "bytes=64"


def test_peer_apps_use_reshaped_routes() -> None:
    """Comparison apps must match the official suite, not the old /json /validate set."""
    names = (
        "stario_app.py",
        "granian_rsgi_app.py",
        "socketify_app.py",
        "robyn_app.py",
        "sanic_app.py",
        "fastapi_app.py",
        "blacksheep_app.py",
        "falcon_app.py",
        "django_bolt/api.py",
    )
    forbidden = ("validate_fields", 'add_route("/json"', 'add_route("/validate"')
    for name in names:
        text = (SERVER_DIR / "apps" / name).read_text()
        for token in forbidden:
            assert token not in text, f"{name} still mentions {token!r}"
        assert "/plaintext" in text
        assert "/echo" in text
        assert "/user/" in text or "/user/:" in text or "/user/{" in text
