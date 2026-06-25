"""Tests for stario.http.request - HTTP request handling."""

import pytest

from stario.exceptions import ClientDisconnected, HttpException, StarioRuntime
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request
from tests.helpers import make_body_reader
from tests.helpers import make_request as _make_request


class TestRequestBasic:
    """Smoke test for request surface."""

    def test_defaults_and_overrides(self):
        req = _make_request()
        assert req.method == "GET"
        assert req.path == "/"
        assert req.protocol_version == "1.1"
        assert req.keep_alive is True

        post = _make_request(method="POST", path="/users/123")
        assert post.method == "POST"
        assert post.path == "/users/123"


class TestRequestCookies:
    """Integration: cookies parsed from request headers."""

    def test_multiple_cookie_headers_merge(self):
        hdrs = Headers()
        hdrs.add("Cookie", "a=1")
        hdrs.add("Cookie", "b=2")
        req = Request(method="GET", path="/", headers=hdrs, body=make_body_reader())
        assert req.cookies == {"a": "1", "b": "2"}


class TestRequestHost:
    def test_host_strips_port(self):
        req = _make_request(headers={"Host": "Example.COM:8080"})
        assert req.host == "example.com"

    def test_host_ipv6_with_port(self):
        req = _make_request(headers={"Host": "[::1]:8000"})
        assert req.host == "[::1]"

    def test_host_strips_whitespace(self):
        req = _make_request(headers={"Host": "  Example.COM:8080  "})
        assert req.host == "example.com"


class TestRequestBody:
    """Test request body handling."""

    async def test_no_body(self):
        req = _make_request()
        body = await req.body()
        assert body == b""

    async def test_body_multiple_reads(self):
        req = _make_request(body=b"data")
        body1 = await req.body()
        body2 = await req.body()
        assert body1 == body2 == b"data"

    async def test_body_max_size_is_per_call_limit(self):
        reader = BodyReader(pause=lambda: None, resume=lambda: None, max_size=10)
        reader.feed(b"hello")
        reader.complete()
        req = Request(method="POST", path="/", headers=Headers(), body=reader)

        with pytest.raises(HttpException) as excinfo:
            await req.body(max_size=4)

        assert excinfo.value.status_code == 413
        assert await req.body(max_size=5) == b"hello"

    async def test_body_none_reader_returns_empty(self):
        req = Request(
            method="GET",
            path="/",
            headers=Headers(),
            body=None,
        )
        assert await req.body() == b""

    async def test_stream_twice_raises_stario_runtime(self):
        reader = BodyReader(pause=lambda: None, resume=lambda: None)
        reader.feed(b"chunk")
        req = Request(method="POST", path="/", headers=Headers(), body=reader)
        stream = req.stream()
        assert await stream.__anext__() == b"chunk"
        with pytest.raises(StarioRuntime, match="already streaming"):
            async for _ in req.stream():
                pass

    async def test_body_then_stream_raises_stario_runtime(self):
        req = _make_request(body=b"data")

        assert await req.body() == b"data"
        assert await req.body() == b"data"
        with pytest.raises(StarioRuntime, match="already read"):
            async for _ in req.stream():
                pass


class TestBodyReaderTimeout:
    async def test_stream_raises_408_when_body_stalls(self):
        """Slowloris guard: a stalled upload times out with 408."""
        reader = BodyReader(
            pause=lambda: None,
            resume=lambda: None,
            timeout=0.01,
        )
        reader.feed(b"partial")

        chunks: list[bytes] = []

        async def drain() -> None:
            async for chunk in reader.stream():
                chunks.append(chunk)

        with pytest.raises(HttpException) as excinfo:
            await drain()

        assert excinfo.value.status_code == 408
        assert chunks == [b"partial"]


class TestBodyReaderFailures:
    async def test_feed_over_limit_mid_stream_raises_413(self):
        reader = BodyReader(
            pause=lambda: None,
            resume=lambda: None,
            max_size=10,
        )
        reader.feed(b"12345")

        stream = reader.stream()
        assert await stream.__anext__() == b"12345"

        reader.feed(b"6789012345x")  # total 16 > 10
        with pytest.raises(HttpException) as excinfo:
            await stream.__anext__()
        assert excinfo.value.status_code == 413

    async def test_read_max_size_failure_leaves_body_retryable(self):
        reader = BodyReader(
            pause=lambda: None,
            resume=lambda: None,
        )
        reader.feed(b"hello")
        reader.complete()

        with pytest.raises(HttpException):
            await reader.read(max_size=3)

        assert await reader.read(max_size=10) == b"hello"

    async def test_stream_raises_client_disconnected_when_abort_called(self):
        reader = BodyReader(
            pause=lambda: None,
            resume=lambda: None,
        )
        reader.abort()

        with pytest.raises(ClientDisconnected, match="request body finished uploading"):
            async for _ in reader.stream():
                pass
