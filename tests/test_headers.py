"""Tests for stario.http.headers - HTTP header handling."""

import pytest

from stario.http.headers import (
    HEADER_LOOKUP,
    VALUE_LOOKUP,
    Headers,
    encode_value,
)


class TestHeaderLookup:
    """Test header name lookup."""

    def test_common_header_fast_path(self):
        assert HEADER_LOOKUP["Content-Type"] == b"content-type"
        assert "Content-Type" in HEADER_LOOKUP

    def test_lowercase_header(self):
        assert HEADER_LOOKUP["content-type"] == b"content-type"

    def test_custom_header(self):
        assert HEADER_LOOKUP["X-Custom-Header"] == b"x-custom-header"

    def test_bytes_header(self):
        assert HEADER_LOOKUP[b"Content-Type"] == b"content-type"

    def test_invalid_header_raises(self):
        with pytest.raises(ValueError, match="Invalid header name"):
            HEADER_LOOKUP["Header\x00Name"]


class TestEncodeValue:
    """Test header value encoding."""

    def test_common_value_fast_path(self):
        result = encode_value("text/html")
        assert result == b"text/html"
        assert "text/html" in VALUE_LOOKUP

    def test_custom_value(self):
        result = encode_value("my-custom-value")
        assert result == b"my-custom-value"

    def test_bytes_value(self):
        result = encode_value(b"text/plain")
        assert result == b"text/plain"


class TestHeaders:
    """Test Headers class."""

    def test_create_empty(self):
        h = Headers()
        assert len(h) == 0

    def test_set_header(self):
        h = Headers()
        h.set("Content-Type", "text/html")
        assert h.get("Content-Type") == "text/html"

    def test_set_lowercase(self):
        h = Headers()
        h.set("content-type", "text/html")
        assert h.get("Content-Type") == "text/html"

    def test_set_bytes(self):
        h = Headers()
        h.set(b"content-type", b"text/html")
        assert h.get(b"content-type") == "text/html"

    def test_add_single(self):
        h = Headers()
        h.add("X-Custom", "value1")
        assert h.get("X-Custom") == "value1"

    def test_add_multiple(self):
        h = Headers()
        h.add("Set-Cookie", "a=1")
        h.add("Set-Cookie", "b=2")

        values = h.getlist("Set-Cookie")
        assert len(values) == 2
        assert "a=1" in values
        assert "b=2" in values

    def test_get_nonexistent(self):
        h = Headers()
        assert h.get("X-Missing") is None

    def test_get_default(self):
        h = Headers()
        assert h.get("X-Missing", "default") == "default"

    def test_getlist_nonexistent(self):
        h = Headers()
        assert h.getlist("X-Missing") == []

    def test_getlist_single(self):
        h = Headers()
        h.set("Content-Type", "text/html")
        assert h.getlist("Content-Type") == ["text/html"]

    def test_update(self):
        h = Headers()
        h.update({"Content-Type": "text/html", "X-Custom": "value"})
        assert h.get("Content-Type") == "text/html"
        assert h.get("X-Custom") == "value"

    def test_update_none(self):
        h = Headers()
        h.update(None)  # Should not raise
        assert len(h) == 0

    def test_setdefault_new(self):
        h = Headers()
        result = h.setdefault("Content-Type", "text/html")
        assert result == "text/html"
        assert h.get("Content-Type") == "text/html"

    def test_setdefault_existing(self):
        h = Headers()
        h.set("Content-Type", "text/plain")
        result = h.setdefault("Content-Type", "text/html")
        assert result == "text/plain"  # Original preserved

    def test_contains(self):
        h = Headers()
        h.set("Content-Type", "text/html")

        assert "Content-Type" in h
        assert "content-type" in h
        assert b"content-type" in h
        assert "X-Missing" not in h

    def test_remove(self):
        h = Headers()
        h.set("Content-Type", "text/html")
        h.remove("Content-Type")
        assert "Content-Type" not in h

    def test_clear(self):
        h = Headers()
        h.set("A", "1")
        h.set("B", "2")
        h.clear()
        assert len(h) == 0

    def test_items(self):
        h = Headers()
        h.set("Content-Type", "text/html")
        h.set("X-Custom", "value")

        items = h.items()
        assert len(items) == 2

        names = [name for name, _ in items]
        assert b"content-type" in names
        assert b"x-custom" in names

    def test_items_multiple_values(self):
        h = Headers()
        h.add("Set-Cookie", "a=1")
        h.add("Set-Cookie", "b=2")

        items = h.items()
        assert len(items) == 2
        # Both should have same header name
        assert all(name == b"set-cookie" for name, _ in items)


class TestHeadersRaw:
    """Test raw header methods (no encoding)."""

    def test_rset(self):
        h = Headers()
        h.rset(b"content-type", b"text/html")
        assert h.get(b"content-type") == "text/html"

    def test_radd(self):
        h = Headers()
        h.radd(b"set-cookie", b"a=1")
        h.radd(b"set-cookie", b"b=2")
        assert len(h.getlist(b"set-cookie")) == 2

    def test_rget(self):
        h = Headers()
        h.rset(b"content-type", b"text/html")
        assert h.rget(b"content-type") == b"text/html"
        assert h.rget(b"x-missing") is None
        assert h.rget(b"x-missing", b"default") == b"default"

    def test_rgetlist(self):
        h = Headers()
        h.radd(b"set-cookie", b"a=1")
        h.radd(b"set-cookie", b"b=2")
        values = h.rgetlist(b"set-cookie")
        assert values == [b"a=1", b"b=2"]
        assert h.rgetlist(b"x-missing") == []


class TestHeaderChaining:
    """Test method chaining."""

    def test_add_returns_self(self):
        h = Headers()
        result = h.add("X-A", "1")
        assert result is h

    def test_set_returns_self(self):
        h = Headers()
        result = h.set("X-A", "1")
        assert result is h

    def test_chaining(self):
        h = Headers()
        h.set("A", "1").set("B", "2").add("C", "3")

        assert h.get("A") == "1"
        assert h.get("B") == "2"
        assert h.get("C") == "3"
