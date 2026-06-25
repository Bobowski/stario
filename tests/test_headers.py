"""Tests for stario.http.headers - HTTP header handling."""

import pytest

from stario.http.headers import (
    Headers,
    encode_header_value,
)


class TestHeaders:
    """Test Headers class."""

    def test_set_lowercase_and_contains(self):
        h = Headers()
        h.set("content-type", "text/html")
        assert h.get("Content-Type") == "text/html"
        assert "Content-Type" in h
        assert "content-type" in h
        assert "X-Missing" not in h
        assert "Bad@Name" not in h

    def test_add_multiple(self):
        h = Headers()
        h.add("Set-Cookie", "a=1")
        h.add("Set-Cookie", "b=2")

        values = h.getlist("Set-Cookie")
        assert len(values) == 2
        assert "a=1" in values
        assert "b=2" in values

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

    def test_remove(self):
        h = Headers()
        h.set("Content-Type", "text/html")
        h.remove("Content-Type")
        assert "Content-Type" not in h

    def test_items(self):
        h = Headers()
        h.set("Content-Type", "text/html")
        h.set("X-Custom", "value")

        items = h.items()
        assert len(items) == 2

        names = [name for name, _ in items]
        assert "content-type" in names
        assert "x-custom" in names

    def test_items_multiple_values(self):
        h = Headers()
        h.add("Set-Cookie", "a=1")
        h.add("Set-Cookie", "b=2")

        items = h.items()
        assert len(items) == 2
        # Both should have same header name
        assert all(name == "set-cookie" for name, _ in items)


class TestHeadersUnsafe:
    """Test unsafe header methods (no encoding/validation)."""

    def test_unsafe_set(self):
        h = Headers()
        h.unsafe_set(b"content-type", b"text/html")
        assert h.get("content-type") == "text/html"

    def test_unsafe_add(self):
        h = Headers()
        h.unsafe_add(b"set-cookie", b"a=1")
        h.unsafe_add(b"set-cookie", b"b=2")
        assert len(h.getlist("set-cookie")) == 2

    def test_unsafe_get(self):
        h = Headers()
        h.unsafe_set(b"content-type", b"text/html")
        assert h.unsafe_get(b"content-type") == b"text/html"
        assert h.unsafe_get(b"x-missing") is None
        assert h.unsafe_get(b"x-missing", b"default") == b"default"

    def test_unsafe_getlist(self):
        h = Headers()
        h.unsafe_add(b"set-cookie", b"a=1")
        h.unsafe_add(b"set-cookie", b"b=2")
        values = h.unsafe_getlist(b"set-cookie")
        assert values == [b"a=1", b"b=2"]
        assert h.unsafe_getlist(b"x-missing") == []

    def test_unsafe_remove(self):
        h = Headers()
        h.unsafe_set(b"x-custom", b"v")
        h.unsafe_remove(b"x-custom")
        assert h.unsafe_get(b"x-custom") is None


class TestHeaderMutators:
    """Test header mutators."""

    def test_set_overwrites_prior_add_values(self):
        h = Headers()
        h.add("X-Custom", "first")
        h.add("X-Custom", "second")
        h.set("X-Custom", "final")
        assert h.getlist("X-Custom") == ["final"]

    def test_constructor_with_raw_header_data(self):
        h = Headers({b"host": b"example.com"})
        assert h.unsafe_get(b"host") == b"example.com"

    def test_remove_missing_is_noop(self):
        h = Headers()
        h.remove("X-Missing")
        assert len(h) == 0


class TestHeaderInjectionContract:
    """Pin the validation boundary between the safe and raw header APIs.

    `set`/`add`/`encode_header_value` MUST reject CRLF so handler
    code can never smuggle extra header lines. The `unsafe_*` methods skip
    validation by design (parser/writer hot paths) — anything reaching them
    must already be validated.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "bad\r\ninjected: 1",
            "bad\rinjected",
            "bad\ninjected",
            "bad\x00null",
        ],
    )
    def test_add_rejects_control_characters_in_value(self, value):
        h = Headers()
        with pytest.raises(ValueError, match="Invalid header value"):
            h.add("X-Custom", value)

    @pytest.mark.parametrize(
        "value",
        ["bad\r\ninjected: 1", "bad\nx", "bad\x00"],
    )
    def test_encode_value_rejects_control_characters(self, value):
        with pytest.raises(ValueError, match="Invalid header value"):
            encode_header_value(value)

    def test_setdefault_rejects_control_characters_in_value(self):
        h = Headers()
        with pytest.raises(ValueError, match="Invalid header value"):
            h.setdefault("X-Custom", "bad\r\ninjected: 1")

    @pytest.mark.parametrize(
        "name",
        ["Bad Name!", "X:Colon", "name\r\n", "X-é"],
    )
    def test_set_rejects_invalid_names(self, name):
        h = Headers()
        with pytest.raises(ValueError, match="Invalid header name"):
            h.set(name, "v")

    def test_unsafe_methods_bypass_validation_by_design(self):
        # unsafe_add/unsafe_set trust their input: they accept bytes that the safe API
        # rejects. This is the documented contract — callers (HTTP parser,
        # response helpers) own validation before reaching the unsafe API.
        h = Headers()
        h.unsafe_add(b"x-raw", b"evil\r\ninjected: 1")
        h.unsafe_set(b"x-raw2", b"evil\r\ninjected: 2")
        assert h.unsafe_get(b"x-raw") == b"evil\r\ninjected: 1"
        assert h.unsafe_get(b"x-raw2") == b"evil\r\ninjected: 2"


class TestHeadersEdgeCases:
    def test_setdefault_with_multi_valued_header_returns_first(self):
        h = Headers()
        h.add("Set-Cookie", "a=1")
        h.add("Set-Cookie", "b=2")
        assert h.setdefault("Set-Cookie", "c=3") == "a=1"
        assert h.getlist("Set-Cookie") == ["a=1", "b=2"]
