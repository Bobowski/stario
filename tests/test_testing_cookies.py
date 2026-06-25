"""Tests for TestClient cookie wire helpers (`stario.testing.cookies`)."""

import stario.cookies as cookies
from stario.testing.cookies import (
    parse_set_cookie_headers,
    serialize_cookie_header,
)


def test_serialize_cookie_header_quotes_when_needed() -> None:
    header = serialize_cookie_header({"x": "a;b", "y": "2"})
    assert cookies.parse_cookie_headers([header]) == {"x": "a;b", "y": "2"}


def test_parse_set_cookie_headers_skips_empty_values() -> None:
    parsed = parse_set_cookie_headers(
        ["sid=abc; Path=/", "theme=; Max-Age=0", "flag=1"]
    )
    assert parsed == {"sid": "abc", "flag": "1"}
