"""Tests for stario.http.request - HTTP request handling."""

from stario.testing import TestRequest


class TestRequestBasic:
    """Test basic request properties."""

    def test_default_values(self):
        req = TestRequest()
        assert req.method == "GET"
        assert req.path == "/"
        assert req.tail == ""
        assert req.protocol_version == "1.1"
        assert req.keep_alive is True

    def test_custom_method(self):
        req = TestRequest(method="POST")
        assert req.method == "POST"

    def test_custom_path(self):
        req = TestRequest(path="/users/123")
        assert req.path == "/users/123"


class TestRequestHeaders:
    """Test request header handling."""

    def test_no_headers(self):
        req = TestRequest()
        assert req.headers.get("X-Missing") is None

    def test_with_headers(self):
        req = TestRequest(headers={"Content-Type": "application/json"})
        assert req.headers.get("Content-Type") == "application/json"

    def test_multiple_headers(self):
        req = TestRequest(
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
        req = TestRequest()
        assert req.query == {}

    def test_simple_query(self):
        req = TestRequest(query={"name": "test"})
        assert req.query.get("name") == "test"

    def test_multiple_params(self):
        req = TestRequest(query={"a": "1", "b": "2", "c": "3"})
        assert req.query.get("a") == "1"
        assert req.query.get("b") == "2"
        assert req.query.get("c") == "3"

    def test_query_get_default(self):
        req = TestRequest()
        assert req.query.get("missing") is None
        assert req.query.get("missing", "fallback") == "fallback"

    def test_query_getlist(self):
        req = TestRequest(query={"tags": ["a", "b", "c"]})
        assert req.query.getlist("tags") == ["a", "b", "c"]
        assert req.query.getlist("missing") == []

    def test_query_contains(self):
        req = TestRequest(query={"page": "1"})
        assert "page" in req.query
        assert "missing" not in req.query

    def test_query_bool_and_len(self):
        empty = TestRequest()
        assert not empty.query
        assert len(empty.query) == 0

        filled = TestRequest(query={"a": "1"})
        assert filled.query
        assert len(filled.query) == 1


class TestRequestCookies:
    """Test cookie parsing."""

    def test_no_cookies(self):
        req = TestRequest()
        assert req.cookies == {}

    def test_single_cookie(self):
        req = TestRequest(headers={"Cookie": "session=abc123"})
        assert req.cookies["session"] == "abc123"

    def test_multiple_cookies(self):
        req = TestRequest(headers={"Cookie": "a=1; b=2; c=3"})
        assert req.cookies["a"] == "1"
        assert req.cookies["b"] == "2"
        assert req.cookies["c"] == "3"

    def test_cookie_with_quotes(self):
        req = TestRequest(headers={"Cookie": 'name="John Doe"'})
        assert req.cookies["name"] == "John Doe"


class TestRequestBody:
    """Test request body handling."""

    async def test_no_body(self):
        req = TestRequest()
        body = await req.body()
        assert body == b""

    async def test_with_body(self):
        req = TestRequest(body=b"Hello, World!")
        body = await req.body()
        assert body == b"Hello, World!"

    async def test_json_body(self):
        req = TestRequest(body=b'{"name": "test", "count": 42}')
        data = await req.json()
        assert data["name"] == "test"
        assert data["count"] == 42


    async def test_body_multiple_reads(self):
        req = TestRequest(body=b"data")
        body1 = await req.body()
        body2 = await req.body()
        assert body1 == body2 == b"data"


class TestRequestStream:
    """Test body streaming."""

    async def test_stream_body(self):
        req = TestRequest(body=b"streaming data")
        chunks = []
        async for chunk in req.stream():
            chunks.append(chunk)
        assert b"".join(chunks) == b"streaming data"
