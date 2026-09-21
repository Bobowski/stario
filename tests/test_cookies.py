"""Tests for `stario.cookies` helpers."""

import asyncio
from datetime import UTC, datetime

import pytest

import stario.cookies as cookies
from stario.http.writer import Writer
from tests.helpers import _MemoryTransport


def _writer() -> tuple[Writer, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    sink = bytearray()

    transport = _MemoryTransport(sink.extend)
    w = Writer(
        transport=transport,
        get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
        on_completed=lambda: None,
    )
    return w, loop


def _cookie_header(w: Writer) -> str:
    lines = w.headers.unsafe_getlist(b"set-cookie")
    return b";".join(lines).decode("latin-1")


def test_expires_int_and_datetime() -> None:
    w, loop = _writer()
    try:
        cookies.set_cookie(w, "sid", "v", expires=1_700_000_000)
        combined = _cookie_header(w).lower()
        assert "expires=" in combined
        assert "1970" not in combined  # not raw "1" as string

        w2, loop2 = _writer()
        try:
            expires = datetime(2030, 1, 15, 12, 0, 0, tzinfo=UTC)
            cookies.set_cookie(w2, "sid", "v", expires=expires)
            combined2 = _cookie_header(w2).lower()
            assert "expires=" in combined2
            assert "2030" in combined2
        finally:
            loop2.close()
    finally:
        loop.close()


def test_set_cookie_max_age_and_defaults() -> None:
    w, loop = _writer()
    try:
        cookies.set_cookie(w, "sid", "v", max_age=3600)
        combined = _cookie_header(w).lower()
        assert "max-age=3600" in combined
        assert "samesite=lax" in combined
        assert "path=/" in combined
    finally:
        loop.close()


def test_set_cookie_httponly_secure_domain_path() -> None:
    w, loop = _writer()
    try:
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
    finally:
        loop.close()


def test_delete_cookie_clears_with_matching_scope() -> None:
    w, loop = _writer()
    try:
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
    finally:
        loop.close()


def test_parse_cookie_headers_quoted_semicolon() -> None:
    parsed = cookies.parse_cookie_headers(['x="a;b"; y=2'])
    assert parsed == {"x": "a;b", "y": "2"}


def test_parse_cookie_headers_later_header_wins() -> None:
    parsed = cookies.parse_cookie_headers(["a=1", "a=2; b=3"])
    assert parsed == {"a": "2", "b": "3"}


@pytest.mark.parametrize("explicit_secure", [False, True])
def test_samesite_none_sets_secure(explicit_secure: bool) -> None:
    w, loop = _writer()
    try:
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
    finally:
        loop.close()
