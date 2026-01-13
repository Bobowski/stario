"""Tests for stario.http.writer via ResponseRecorder."""

import pytest

from stario.html import H1
from stario.testing import ResponseRecorder


class TestResponseRecorderBasic:
    """Test basic ResponseRecorder functionality."""

    def test_default_status(self):
        w = ResponseRecorder()
        assert w.status_code == 200

    def test_not_started_initially(self):
        w = ResponseRecorder()
        assert not w.started
        assert not w.closed


class TestResponseRecorderText:
    """Test text response methods."""

    def test_text_response(self):
        w = ResponseRecorder()
        w.text("Hello, World!")

        assert w.status_code == 200
        assert w.text_body() == "Hello, World!"
        assert w.headers["content-type"] == "text/plain; charset=utf-8"

    def test_text_custom_status(self):
        w = ResponseRecorder()
        w.text("Not Found", 404)

        assert w.status_code == 404

    def test_html_response(self):
        w = ResponseRecorder()
        w.html(H1("Hello"))

        assert w.status_code == 200
        assert w.text_body() == "<h1>Hello</h1>"
        assert w.headers["content-type"] == "text/html; charset=utf-8"

    def test_html_custom_status(self):
        w = ResponseRecorder()
        w.html(H1("Error"), 500)

        assert w.status_code == 500


class TestResponseRecorderJson:
    """Test JSON response methods."""

    def test_json_dict(self):
        w = ResponseRecorder()
        w.json({"message": "hello"})

        assert w.status_code == 200
        assert w.json_body() == {"message": "hello"}
        assert w.headers["content-type"] == "application/json; charset=utf-8"

    def test_json_list(self):
        w = ResponseRecorder()
        w.json([1, 2, 3])

        assert w.json_body() == [1, 2, 3]

    def test_json_nested(self):
        w = ResponseRecorder()
        w.json({"users": [{"id": 1}, {"id": 2}]})

        data = w.json_body()
        assert len(data["users"]) == 2

    def test_json_unicode(self):
        w = ResponseRecorder()
        w.json({"emoji": "🎉"})

        assert w.json_body()["emoji"] == "🎉"


class TestResponseRecorderRedirect:
    """Test redirect responses."""

    def test_redirect_default(self):
        w = ResponseRecorder()
        w.redirect("/new-location")

        assert w.status_code == 307
        assert w.headers["location"] == "/new-location"

    def test_redirect_permanent(self):
        w = ResponseRecorder()
        w.redirect("/permanent", 301)

        assert w.status_code == 301

    def test_redirect_see_other(self):
        w = ResponseRecorder()
        w.redirect("/other", 303)

        assert w.status_code == 303


class TestResponseRecorderEmpty:
    """Test empty responses."""

    def test_empty_default(self):
        w = ResponseRecorder()
        w.empty()

        assert w.status_code == 204
        assert w.body == b""

    def test_empty_not_modified(self):
        w = ResponseRecorder()
        w.empty(304)

        assert w.status_code == 304


class TestResponseRecorderRespond:
    """Test generic respond method."""

    def test_respond_basic(self):
        w = ResponseRecorder()
        w.respond(b"raw bytes", "application/octet-stream")

        assert w.body == b"raw bytes"
        assert w.headers["content-type"] == "application/octet-stream"

    def test_respond_with_status(self):
        w = ResponseRecorder()
        w.respond(b"created", "text/plain", 201)

        assert w.status_code == 201


class TestResponseRecorderHeaders:
    """Test header handling."""

    def test_header_method(self):
        w = ResponseRecorder()
        w.header("X-Custom", "value")
        w.text("ok")

        assert w.headers["x-custom"] == "value"

    def test_header_bytes(self):
        w = ResponseRecorder()
        w.header(b"X-Custom", b"value")
        w.text("ok")

        assert w.headers["x-custom"] == "value"


class TestResponseRecorderErrors:
    """Test error conditions."""

    def test_cannot_set_status_after_started(self):
        w = ResponseRecorder()
        w.text("ok")

        with pytest.raises(RuntimeError, match="after response started"):
            w.status(201)

    def test_cannot_set_header_after_started(self):
        w = ResponseRecorder()
        w.text("ok")

        with pytest.raises(RuntimeError, match="after response started"):
            w.header("X-Late", "value")

    def test_cannot_send_twice(self):
        w = ResponseRecorder()
        w.text("first")

        with pytest.raises(RuntimeError, match="already started"):
            w.text("second")


class TestResponseRecorderAssertions:
    """Test assertion helper methods."""

    def test_assert_status_pass(self):
        w = ResponseRecorder()
        w.text("ok")

        w.assert_status(200)  # Should not raise

    def test_assert_status_fail(self):
        w = ResponseRecorder()
        w.text("ok", 201)

        with pytest.raises(AssertionError):
            w.assert_status(200)

    def test_assert_json_pass(self):
        w = ResponseRecorder()
        w.json({"key": "value"})

        w.assert_json({"key": "value"})  # Should not raise

    def test_assert_json_fail(self):
        w = ResponseRecorder()
        w.json({"key": "value"})

        with pytest.raises(AssertionError):
            w.assert_json({"key": "other"})

    def test_assert_header_pass(self):
        w = ResponseRecorder()
        w.header("X-Custom", "expected")
        w.text("ok")

        w.assert_header("X-Custom", "expected")

    def test_assert_header_fail(self):
        w = ResponseRecorder()
        w.header("X-Custom", "actual")
        w.text("ok")

        with pytest.raises(AssertionError):
            w.assert_header("X-Custom", "expected")


class TestResponseRecorderStreaming:
    """Test streaming functionality."""

    def test_write_bytes(self):
        w = ResponseRecorder()
        w.write(b"chunk1")
        w.write(b"chunk2")
        w.close()

        assert w.body == b"chunk1chunk2"
        assert w.started
        assert w.closed

    def test_write_after_close_raises(self):
        w = ResponseRecorder()
        w.write(b"data")
        w.close()

        with pytest.raises(RuntimeError, match="already closed"):
            w.write(b"more")


class TestResponseRecorderSSE:
    """Test SSE/Datastar event recording."""

    def test_patch_event(self):
        from stario.html import Div

        w = ResponseRecorder()
        w.patch(Div("content"))

        assert len(w.datastar_events) == 1
        assert w.datastar_events[0]["type"] == "patch"

    def test_sync_event(self):
        """Test sync() method."""
        w = ResponseRecorder()
        w.sync({"count": 42})

        assert len(w.datastar_events) == 1
        assert w.datastar_events[0]["type"] == "sync"
        assert w.datastar_events[0]["data"] == {"count": 42}

    def test_multiple_sync_events(self):
        """Test multiple sync events can be sent."""
        w = ResponseRecorder()
        w.sync({"count": 1})
        w.sync({"count": 2})

        assert len(w.datastar_events) == 2
        assert w.datastar_events[0]["data"] == {"count": 1}
        assert w.datastar_events[1]["data"] == {"count": 2}

    def test_navigate_event(self):
        w = ResponseRecorder()
        w.navigate("/new-page")

        assert len(w.datastar_events) == 1
        assert w.datastar_events[0]["type"] == "navigate"
        assert w.datastar_events[0]["url"] == "/new-page"

    def test_remove_event(self):
        w = ResponseRecorder()
        w.remove("#old-element")

        assert len(w.datastar_events) == 1
        assert w.datastar_events[0]["type"] == "remove"
        assert w.datastar_events[0]["selector"] == "#old-element"

    def test_sse_sets_headers(self):
        w = ResponseRecorder()
        w.sync({"x": 1})

        assert w.headers["content-type"] == "text/event-stream"
        assert w.headers["cache-control"] == "no-cache"

    def test_cannot_oneshot_after_sse(self):
        w = ResponseRecorder()
        w.sync({"x": 1})

        with pytest.raises(RuntimeError, match="after SSE streaming"):
            w.json({"error": "too late"})


class TestResponseRecorderCookies:
    """Test cookie handling."""

    def test_set_cookie_basic(self):
        w = ResponseRecorder()
        w.cookie("session", "abc123")
        w.text("ok")

        assert "set-cookie" in w.headers
        assert "session=abc123" in w.headers["set-cookie"]

    def test_set_cookie_with_options(self):
        w = ResponseRecorder()
        w.cookie(
            "session",
            "abc123",
            max_age=3600,
            httponly=True,
            secure=True,
            samesite="strict",
        )
        w.text("ok")

        cookie = w.headers["set-cookie"]
        assert "session=abc123" in cookie
        assert "Max-Age=3600" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=strict" in cookie

    def test_set_cookie_with_path(self):
        w = ResponseRecorder()
        w.cookie("pref", "dark", path="/app")
        w.text("ok")

        assert "Path=/app" in w.headers["set-cookie"]

    def test_delete_cookie(self):
        w = ResponseRecorder()
        w.delete_cookie("session")
        w.text("ok")

        cookie = w.headers["set-cookie"]
        assert "session=" in cookie
        assert "Max-Age=0" in cookie
        # Should have past expiration date
        assert "1970" in cookie

    def test_delete_cookie_with_path(self):
        w = ResponseRecorder()
        w.delete_cookie("session", path="/app")
        w.text("ok")

        cookie = w.headers["set-cookie"]
        assert "Path=/app" in cookie
        assert "Max-Age=0" in cookie

    def test_multiple_cookies(self):
        w = ResponseRecorder()
        w.cookie("a", "1")
        w.cookie("b", "2")
        w.text("ok")

        cookie = w.headers["set-cookie"]
        assert "a=1" in cookie
        assert "b=2" in cookie
