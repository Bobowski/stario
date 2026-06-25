"""Tests for wire-level path/method decoding."""

import pytest

from stario.http.wire import (
    _PATH_CACHE_MAX_BYTES,
    _decode_path_cached,
    decode_method,
    decode_path,
)


class TestDecodeMethod:
    def test_known_verbs(self) -> None:
        assert decode_method(b"GET") == "GET"
        assert decode_method(b"POST") == "POST"

    def test_invalid_bytes(self) -> None:
        with pytest.raises(UnicodeDecodeError):
            decode_method(b"\xff")


class TestDecodePath:
    def setup_method(self) -> None:
        _decode_path_cached.cache_clear()

    def test_plain_path(self) -> None:
        assert decode_path(b"/users") == "/users"
        assert decode_path(b"/") == "/"

    def test_percent_encoded_space(self) -> None:
        assert decode_path(b"/hello%20world") == "/hello world"

    def test_encoded_slash_stays_encoded(self) -> None:
        assert decode_path(b"/files/a%2Fb") == "/files/a%2Fb"

    def test_invalid_percent_encoding(self) -> None:
        with pytest.raises(ValueError, match="invalid percent-encoding"):
            decode_path(b"/bad%")

        with pytest.raises(ValueError, match="invalid percent-encoding"):
            decode_path(b"/bad%zz")

    def test_nul_byte_rejected(self) -> None:
        with pytest.raises(ValueError, match="NUL byte"):
            decode_path(b"/%00")

    def test_invalid_utf8_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid UTF-8"):
            decode_path(b"/%C3%28")

    def test_long_path_bypasses_cache(self) -> None:
        long_segment = b"a" * (_PATH_CACHE_MAX_BYTES + 1)
        path = b"/" + long_segment
        assert decode_path(path) == "/" + long_segment.decode("ascii")
        assert _decode_path_cached.cache_info().misses == 0

    def test_cache_hit(self) -> None:
        path = b"/cached"
        assert decode_path(path) == "/cached"
        info = _decode_path_cached.cache_info()
        assert info.misses == 1
        assert decode_path(path) == "/cached"
        assert _decode_path_cached.cache_info().hits == 1
