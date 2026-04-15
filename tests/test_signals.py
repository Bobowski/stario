"""Tests for Datastar signal parsing via ``stario.datastar.read_signals``."""

import json
from urllib.parse import urlencode

import pytest

from stario import datastar as ds
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
        hdrs.update(headers)

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

        result = await ds.read_signals(req)

        assert result == {"name": "test", "count": 42}

    async def test_reads_get_query_datastar_payload(self):
        req = _make_request(method="GET", query={"datastar": '{"name":"test","count":42}'})

        result = await ds.read_signals(req)

        assert result == {"name": "test", "count": 42}

    async def test_missing_payload_defaults_to_empty_dict(self):
        req = _make_request(method="GET")

        result = await ds.read_signals(req)

        assert result == {}

    async def test_invalid_json_raises_json_decode_error(self):
        req = _make_request(method="POST", body=b"{invalid")

        with pytest.raises(json.JSONDecodeError):
            await ds.read_signals(req)

    async def test_non_object_json_raises_type_error(self):
        req = _make_request(method="POST", body=b"[1,2,3]")

        with pytest.raises(TypeError, match="Signals must decode to a JSON object"):
            await ds.read_signals(req)
