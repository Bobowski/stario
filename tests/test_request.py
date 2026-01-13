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
        assert req.headers.get("Content-Type") == b"application/json"

    def test_multiple_headers(self):
        req = TestRequest(
            headers={
                "Content-Type": "application/json",
                "Accept": "text/html",
                "X-Custom": "value",
            }
        )
        assert req.headers.get("Content-Type") == b"application/json"
        assert req.headers.get("Accept") == b"text/html"
        assert req.headers.get("X-Custom") == b"value"


class TestRequestQuery:
    """Test query string parsing."""

    def test_no_query(self):
        req = TestRequest()
        assert req.query == {}

    def test_simple_query(self):
        req = TestRequest(query={"name": "test"})
        assert req.query["name"] == "test"

    def test_multiple_params(self):
        req = TestRequest(query={"a": "1", "b": "2", "c": "3"})
        # Query values are always strings
        assert req.query["a"] == "1"
        assert req.query["b"] == "2"
        assert req.query["c"] == "3"

    def test_query_args_list(self):
        req = TestRequest(query={"tags": ["a", "b", "c"]})
        # query_args returns list of tuples
        args = req.query_args
        tag_values = [v for k, v in args if k == "tags"]
        assert "a" in tag_values
        assert "b" in tag_values
        assert "c" in tag_values


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
