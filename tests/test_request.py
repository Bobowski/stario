"""Tests for stario.http.request - HTTP request handling."""

import asyncio
from urllib.parse import urlencode

import pytest

from stario.exceptions import ClientDisconnected, HttpException
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


class TestRequestBasic:
    """Test basic request properties."""

    def test_default_values(self):
        req = _make_request()
        assert req.method == "GET"
        assert req.path == "/"
        assert req.protocol_version == "1.1"
        assert req.keep_alive is True

    def test_custom_method(self):
        req = _make_request(method="POST")
        assert req.method == "POST"

    def test_custom_path(self):
        req = _make_request(path="/users/123")
        assert req.path == "/users/123"


class TestRequestHeaders:
    """Test request header handling."""

    def test_no_headers(self):
        req = _make_request()
        assert req.headers.get("X-Missing") is None

    def test_with_headers(self):
        req = _make_request(headers={"Content-Type": "application/json"})
        assert req.headers.get("Content-Type") == "application/json"

    def test_multiple_headers(self):
        req = _make_request(
            headers={
                "Content-Type": "application/json",
                "Accept": "text/html",
                "X-Custom": "value",
            }
        )
        assert req.headers.get("Content-Type") == "application/json"
        assert req.headers.get("Accept") == "text/html"
        assert req.headers.get("X-Custom") == "value"


class TestRequestQuery:
    """Test query string parsing."""

    def test_no_query(self):
        req = _make_request()
        assert req.query == {}

    def test_simple_query(self):
        req = _make_request(query={"name": "test"})
        assert req.query.get("name") == "test"
        assert req.query_bytes == b"name=test"

    def test_query_bytes_empty(self):
        req = _make_request()
        assert req.query_bytes == b""

    def test_multiple_params(self):
        req = _make_request(query={"a": "1", "b": "2", "c": "3"})
        assert req.query.get("a") == "1"
        assert req.query.get("b") == "2"
        assert req.query.get("c") == "3"

    def test_query_get_default(self):
        req = _make_request()
        assert req.query.get("missing") is None
        assert req.query.get("missing", "fallback") == "fallback"

    def test_query_getlist(self):
        req = _make_request(query={"tags": ["a", "b", "c"]})
        assert req.query.getlist("tags") == ["a", "b", "c"]
        assert req.query.getlist("missing") == []

    def test_query_as_dict(self):
        req = _make_request(query={"a": "1", "b": "2"})
        assert req.query.as_dict() == {"a": "1", "b": "2"}
        assert req.query.as_dict(last=False) == {"a": "1", "b": "2"}

    def test_query_as_dict_repeated_last_wins(self):
        req = _make_request(query={"a": ["1", "2", "3"]})
        assert req.query.as_dict() == {"a": "3"}
        assert req.query.as_dict(last=False) == {"a": "1"}

    def test_query_as_lists(self):
        req = _make_request(query={"tags": ["a", "b", "c"], "page": "1"})
        assert req.query.as_lists() == {"tags": ["a", "b", "c"], "page": ["1"]}

    def test_query_contains(self):
        req = _make_request(query={"page": "1"})
        assert "page" in req.query
        assert "missing" not in req.query

    def test_query_bool_and_len(self):
        empty = _make_request()
        assert not empty.query
        assert len(empty.query) == 0

        filled = _make_request(query={"a": "1"})
        assert filled.query
        assert len(filled.query) == 1


class TestRequestCookies:
    """Test cookie parsing."""

    def test_no_cookies(self):
        req = _make_request()
        assert req.cookies == {}

    def test_single_cookie(self):
        req = _make_request(headers={"Cookie": "session=abc123"})
        assert req.cookies["session"] == "abc123"

    def test_multiple_cookies(self):
        req = _make_request(headers={"Cookie": "a=1; b=2; c=3"})
        assert req.cookies["a"] == "1"
        assert req.cookies["b"] == "2"
        assert req.cookies["c"] == "3"

    def test_cookie_with_quotes(self):
        req = _make_request(headers={"Cookie": 'name="John Doe"'})
        assert req.cookies["name"] == "John Doe"


class TestRequestBody:
    """Test request body handling."""

    async def test_no_body(self):
        req = _make_request()
        body = await req.body()
        assert body == b""

    async def test_with_body(self):
        req = _make_request(body=b"Hello, World!")
        body = await req.body()
        assert body == b"Hello, World!"

    async def test_body_multiple_reads(self):
        req = _make_request(body=b"data")
        body1 = await req.body()
        body2 = await req.body()
        assert body1 == body2 == b"data"


class TestRequestStream:
    """Test body streaming."""

    async def test_stream_body(self):
        req = _make_request(body=b"streaming data")
        chunks = []
        async for chunk in req.stream():
            chunks.append(chunk)
        assert b"".join(chunks) == b"streaming data"


class TestBodyReaderFailures:
    async def test_stream_raises_413_when_body_exceeds_limit(self):
        reader = BodyReader(
            pause=lambda: None,
            resume=lambda: None,
            max_size=3,
        )
        reader.feed(b"toolarge")

        with pytest.raises(HttpException, match="Request body too large"):
            async for _ in reader.stream():
                pass

    async def test_stream_raises_client_disconnected_when_abort_called(self):
        reader = BodyReader(
            pause=lambda: None,
            resume=lambda: None,
        )
        reader.abort()

        with pytest.raises(ClientDisconnected, match="Client disconnected"):
            async for _ in reader.stream():
                pass

    async def test_stream_raises_client_disconnected_when_disconnect_future_finishes(self):
        loop = asyncio.get_running_loop()
        disconnect = loop.create_future()
        reader = BodyReader(
            pause=lambda: None,
            resume=lambda: None,
            disconnect=disconnect,
        )
        disconnect.set_result(None)

        with pytest.raises(ClientDisconnected, match="Client disconnected"):
            async for _ in reader.stream():
                pass
