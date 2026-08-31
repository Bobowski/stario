"""Tests for the request view used by handlers (Cython Request / TestRequest)."""

import pytest

from stario.exceptions import RequestBodyError
from stario.http.headers import Headers
from stario.http.request import Request
from stario.testing.harness import TestRequest
from tests.helpers import make_request as _make_request


class TestRequestBasic:
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
    def test_multiple_cookie_headers_merge(self):
        hdrs = Headers()
        hdrs.add("Cookie", "a=1")
        hdrs.add("Cookie", "b=2")
        req = TestRequest(method="GET", path="/", headers=hdrs)
        assert req.cookies == {"a": "1", "b": "2"}
        assert req.cookies.get("a") == "1"
        assert "b" in req.cookies

    def test_get_does_not_require_as_dict(self):
        hdrs = Headers()
        hdrs.add("Cookie", "session=abc; unused=1; unused=2")
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("session") == "abc"
        assert req.cookies.get("missing") is None
        assert "session" in req.cookies
        assert "unused" in req.cookies
        assert "missing" not in req.cookies

    def test_get_later_header_and_pair_win(self):
        hdrs = Headers()
        hdrs.add("Cookie", "a=1; session=old")
        hdrs.add("Cookie", "a=2; session=new; a=3")
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("session") == "new"
        assert req.cookies.get("a") == "3"

    def test_get_quoted_semicolon_value(self):
        hdrs = Headers()
        hdrs.add("Cookie", 'x="a;b"; y=2')
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("x") == "a;b"
        assert req.cookies.get("y") == "2"

    def test_empty_value_is_present(self):
        hdrs = Headers()
        hdrs.add("Cookie", "session=")
        req = Request(method="GET", path="/", headers=hdrs, body=b"")
        assert req.cookies.get("session") == ""
        assert "session" in req.cookies
        assert bool(req.cookies) is True


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
        req = TestRequest(method="POST", path="/", body=b"hello")

        with pytest.raises(RequestBodyError) as excinfo:
            await req.body(max_size=4)

        assert excinfo.value.status_code == 413
        assert await req.body(max_size=5) == b"hello"

    async def test_body_none_returns_empty(self):
        req = TestRequest(method="GET", path="/", body=b"")
        assert await req.body() == b""

    async def test_stream_then_stream_raises(self):
        req = TestRequest(method="POST", path="/", body=b"chunk")
        stream = req.stream()
        assert await stream.__anext__() == b"chunk"
        with pytest.raises(RuntimeError, match="already streaming"):
            async for _ in req.stream():
                pass
