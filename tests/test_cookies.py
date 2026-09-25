"""Tests for `stario.cookies` helpers."""

from datetime import UTC, datetime

import pytest

import stario.cookies as cookies
from stario.exceptions import StarioError
from tests.helpers import make_writer


def _cookie_header(w) -> str:
    lines = w.headers.unsafe_getlist(b"set-cookie")
    return b";".join(lines).decode("latin-1")


def test_expires_int_and_datetime() -> None:
    w = make_writer()
    cookies.set_cookie(w, "sid", "v", expires=1_700_000_000)
    combined = _cookie_header(w).lower()
    assert "expires=" in combined
    assert "1970" not in combined

    w2 = make_writer()
    expires = datetime(2030, 1, 15, 12, 0, 0, tzinfo=UTC)
    cookies.set_cookie(w2, "sid", "v", expires=expires)
    combined2 = _cookie_header(w2).lower()
    assert "expires=" in combined2
    assert "2030" in combined2


def test_set_cookie_max_age_and_defaults() -> None:
    w = make_writer()
    cookies.set_cookie(w, "sid", "v", max_age=3600)
    combined = _cookie_header(w).lower()
    assert "max-age=3600" in combined
    assert "samesite=lax" in combined
    assert "path=/" in combined


def test_set_cookie_httponly_secure_domain_path() -> None:
    w = make_writer()
    cookies.set_cookie(
        w,
        "sid",
        "v",
        httponly=True,
        secure=True,
        domain="example.com",
        path="/app",
    )
    combined = _cookie_header(w).lower()
    assert "httponly" in combined
    assert "secure" in combined
    assert "domain=example.com" in combined
    assert "path=/app" in combined


def test_delete_cookie_clears_with_matching_scope() -> None:
    w = make_writer()
    cookies.delete_cookie(
        w,
        "sid",
        path="/app",
        domain="example.com",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    combined = _cookie_header(w).lower()
    assert "sid=" in combined
    assert "max-age=0" in combined
    assert "path=/app" in combined
    assert "domain=example.com" in combined
    assert "secure" in combined
    assert "httponly" in combined
    assert "samesite=strict" in combined


def test_parse_cookie_headers_quoted_semicolon() -> None:
    parsed = cookies.parse_cookie_headers(['x="a;b"; y=2'])
    assert parsed == {"x": "a;b", "y": "2"}


def test_parse_cookie_headers_later_header_wins() -> None:
    parsed = cookies.parse_cookie_headers(["a=1", "a=2; b=3"])
    assert parsed == {"a": "2", "b": "3"}


@pytest.mark.parametrize("explicit_secure", [False, True])
def test_samesite_none_sets_secure(explicit_secure: bool) -> None:
    w = make_writer()
    cookies.set_cookie(
        w,
        "sid",
        "v",
        samesite="none",
        secure=explicit_secure,
    )
    lines = w.headers.unsafe_getlist(b"set-cookie")
    assert lines
    combined = b";".join(lines).decode("latin-1").lower()
    assert "samesite=none" in combined
    assert "secure" in combined


def test_set_cookie_rejects_semicolon_in_path() -> None:
    w = make_writer()
    with pytest.raises(StarioError, match="Invalid cookie path"):
        cookies.set_cookie(w, "sid", "v", path="/; HttpOnly")


def test_set_cookie_rejects_comma_in_domain() -> None:
    w = make_writer()
    with pytest.raises(StarioError, match="Invalid cookie domain"):
        cookies.set_cookie(w, "sid", "v", domain="example.com, evil.test")
