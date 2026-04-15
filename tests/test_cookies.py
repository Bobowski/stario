"""Tests for ``stario.cookies`` helpers."""

import asyncio

import pytest

import stario.cookies as cookies
from stario.http.writer import Writer


def _writer() -> tuple[Writer, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    sink = bytearray()

    w = Writer(
        transport_write=sink.extend,
        get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
        on_completed=lambda: None,
        disconnect=loop.create_future(),
        shutdown=loop.create_future(),
    )
    return w, loop


def test_expires_int_is_unix_timestamp_utc() -> None:
    w, loop = _writer()
    try:
        cookies.set_cookie(w, "sid", "v", expires=1_700_000_000)
        lines = w.headers.rgetlist(b"set-cookie")
        combined = b";".join(lines).decode("latin-1")
        assert "expires=" in combined.lower()
        assert "1970" not in combined  # not raw "1" as string
    finally:
        loop.close()


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
        lines = w.headers.rgetlist(b"set-cookie")
        assert lines
        combined = b";".join(lines).decode("latin-1").lower()
        assert "samesite=none" in combined
        assert "secure" in combined
    finally:
        loop.close()
