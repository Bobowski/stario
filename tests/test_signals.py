"""Tests for Datastar signal parsing via `stario.datastar.read_signals`."""

from urllib.parse import urlencode

import pytest

from stario.datastar import read_signals
from stario.exceptions import StarioError
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request


def _make_request(
    *,
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: dict[str, object] | None = None,
) -> Request:
    hdrs = Headers()
    if headers:
        for name, value in headers.items():
            hdrs.set(name, value)

    reader = BodyReader(
        pause=lambda: None,
        resume=lambda: None,
        disconnect=None,
    )
    reader._cached = body
    reader._complete = True

    return Request(
        method=method,
        path=path,
        query_bytes=urlencode(query or {}, doseq=True).encode("ascii"),
        headers=hdrs,
        body=reader,
    )


class TestReadSignals:
    async def test_returns_dict_for_valid_post_body(self):
        req = _make_request(method="POST", body=b'{"name":"test","count":42}')

        result = await read_signals(req)

        assert result == {"name": "test", "count": 42}

    async def test_reads_get_query_datastar_payload(self):
        req = _make_request(
            method="GET", query={"datastar": '{"name":"test","count":42}'}
        )

        result = await read_signals(req)

        assert result == {"name": "test", "count": 42}

    async def test_delete_ignores_body_uses_query(self):
        req = _make_request(
            method="DELETE",
            query={"datastar": '{"from":"query"}'},
            body=b'{"from":"body"}',
        )

        result = await read_signals(req)

        assert result == {"from": "query"}

    async def test_missing_payload_defaults_to_empty_dict(self):
        req = _make_request(method="GET")

        result = await read_signals(req)

        assert result == {}

    async def test_invalid_json_raises_stario_error(self):
        req = _make_request(method="POST", body=b"{invalid")

        with pytest.raises(StarioError, match="valid JSON"):
            await read_signals(req)

    @pytest.mark.parametrize("payload", [b"[1,2,3]", b"null", b'"text"', b"42"])
    async def test_non_object_json_raises_stario_error(self, payload: bytes):
        req = _make_request(method="POST", body=payload)

        with pytest.raises(StarioError, match="Signals must decode to a JSON object"):
            await read_signals(req)

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
    async def test_write_methods_use_body_not_query(self, method: str):
        req = _make_request(
            method=method,
            query={"datastar": '{"from":"query"}'},
            body=b'{"from":"body"}',
        )

        result = await read_signals(req)

        assert result == {"from": "body"}
