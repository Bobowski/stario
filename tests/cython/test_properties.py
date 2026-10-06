"""Property-based coverage for the Cython core.

Each ``@given`` example drives real requests through ``running_server`` on
one keep-alive connection, so pooled exchanges, the body buffers, and the
response framer all get exercised with random shapes — not just the cases
we thought of by hand.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote, quote_plus

from hypothesis import given, settings
from hypothesis import strategies as st

import stario.responses as responses
from stario import App, Route
from stario_cython.exchange import ParsedQuery
from tests.cython.http import read_chunk, read_response, running_server

_TEXT = st.characters(
    min_codepoint=0x21, max_codepoint=0x10FFFF, blacklist_categories=("Cs",)
)


def _dechunk_response(raw: bytes) -> bytes:
    body = raw.split(b"\r\n\r\n", 1)[1]
    out = b""
    while True:
        size_line, body = body.split(b"\r\n", 1)
        size = int(size_line, 16)
        if size == 0:
            return out
        out += body[:size]
        body = body[size + 2 :]


@given(items=st.lists(st.tuples(st.text(_TEXT), st.text(_TEXT)), max_size=32))
@settings(max_examples=60, deadline=None)
def test_query_wellformed_pairs_roundtrip(items):
    raw = "&".join(f"{quote_plus(k)}={quote_plus(v)}" for k, v in items).encode()
    assert ParsedQuery(raw).items() == items


@given(raw=st.binary(max_size=256))
@settings(max_examples=200, deadline=None)
def test_query_arbitrary_bytes_invariants(raw):
    q = ParsedQuery(raw)
    try:
        items = q.items()
    except ValueError as exc:
        # NUL bytes in a query are a deliberate rejection, not a crash.
        if "NUL" not in str(exc) or (b"\x00" not in raw and b"%00" not in raw.lower()):
            raise
        return
    assert len(q) == len({k for k, _v in items})
    assert bool(q) == bool(items)
    for key, _value in items:
        assert key in q
        assert q.get(key) == next(v for k, v in items if k == key)
        assert q.getlist(key) == [v for k, v in items if k == key]
    assert q.get("missing", "d") == "d"


async def _echo_body(c, w):
    w.respond(await c.req.body(), b"application/octet-stream")


@given(bodies=st.lists(st.binary(max_size=200 * 1024), min_size=2, max_size=12))
@settings(max_examples=15, deadline=None)
def test_post_content_length_bodies_echo(bodies):
    """Known-length bodies of every size echo byte-for-byte."""

    async def run():
        app = App()
        app.add(Route("POST /"), _echo_body)
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                for body in bodies:
                    writer.write(
                        b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n"
                        % len(body)
                        + body
                    )
                    await writer.drain()
                    raw = await read_response(reader)
                    assert raw.split(b"\r\n\r\n", 1)[1] == body
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(run())


@given(
    cases=st.lists(
        st.lists(st.binary(min_size=1, max_size=8192), min_size=1, max_size=8),
        min_size=2,
        max_size=8,
    )
)
@settings(max_examples=15, deadline=None)
def test_post_chunked_bodies_echo(cases):
    """Random chunked-transfer splits still assemble to the same body."""

    async def run():
        app = App()
        app.add(Route("POST /"), _echo_body)
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                for chunks in cases:
                    head = b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
                    frames = b"".join(b"%x\r\n%s\r\n" % (len(c), c) for c in chunks)
                    writer.write(head + frames + b"0\r\n\r\n")
                    await writer.drain()
                    raw = await read_response(reader)
                    assert raw.split(b"\r\n\r\n", 1)[1] == b"".join(chunks)
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(run())


_PART_KIND = st.integers(0, 2)


@given(
    cases=st.lists(
        st.lists(
            st.tuples(_PART_KIND, st.binary(min_size=1, max_size=4096)),
            min_size=1,
            max_size=8,
        ),
        min_size=2,
        max_size=6,
    )
)
@settings(max_examples=15, deadline=None)
def test_chunked_response_roundtrip(cases):
    """write() of bytes / bytearray / memoryview parts lands exactly on the wire.

    The bytearray parts are mutated after ``w.write`` returns — the transport
    may retain the object, so the wire must carry the pre-mutation snapshot.
    """

    async def run():
        app = App()
        pending = iter(cases)

        async def page(_c, w):
            w.headers.set("content-type", "application/octet-stream")
            for kind, data in next(pending):
                if kind == 0:
                    w.write(data)
                elif kind == 1:
                    ba = bytearray(data)
                    w.write(ba)
                    ba[:] = b"\x00" * len(ba)
                else:
                    w.write(memoryview(data))
            w.end()

        app.add(Route("GET /"), page)
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                for parts in cases:
                    writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
                    await writer.drain()
                    head = await reader.readuntil(b"\r\n\r\n")
                    assert b"transfer-encoding: chunked" in head.lower()
                    body = b""
                    while True:
                        chunk = await read_chunk(reader)
                        if not chunk:
                            break
                        body += chunk
                    assert body == b"".join(data for _k, data in parts)
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(run())


_NAME_ALPHABET = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~")
_HOP_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "te", "expect"}
)


@given(
    headers=st.lists(
        st.tuples(
            st.text(_NAME_ALPHABET, min_size=1, max_size=24).filter(
                lambda n: n.lower() not in _HOP_HEADERS
            ),
            st.text(st.characters(min_codepoint=0x21, max_codepoint=0x7E), max_size=48),
        ),
        min_size=1,
        max_size=8,
    )
)
@settings(max_examples=25, deadline=None)
def test_request_headers_pass_through(headers):
    """Every legal request header arrives verbatim on ``c.req.headers``."""

    async def run():
        app = App()
        pending = iter([headers])

        async def page(c, w):
            got = {name: c.req.headers.getlist(name) for name, _v in next(pending)}
            w.respond(json.dumps(got).encode(), b"application/json")

        app.add(Route("GET /"), page)
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                head = b"GET / HTTP/1.1\r\nHost: t\r\n" + b"".join(
                    b"%s: %s\r\n" % (k.encode(), v.encode()) for k, v in headers
                )
                writer.write(head + b"\r\n")
                await writer.drain()
                raw = await read_response(reader)
                got = json.loads(raw.split(b"\r\n\r\n", 1)[1])
                for k, _v in headers:
                    want = [v.strip() for n, v in headers if n == k]
                    assert got.get(k) == want, (k, want, got)
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(run())


@given(
    seg=st.text(
        st.characters(
            min_codepoint=0x21,
            max_codepoint=0x10FFFF,
            blacklist_categories=("Cs", "Cc"),
            blacklist_characters="/?#",
        ),
        min_size=1,
        max_size=24,
    )
)
@settings(max_examples=40, deadline=None)
def test_path_param_percent_decoding(seg):
    """A percent-encoded segment decodes to the original param value."""

    async def run():
        app = App()

        async def page(c, w):
            responses.text(w, c.match.params["id"])

        app.add(Route("GET /u/{id}"), page)
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(
                    b"GET /u/%s HTTP/1.1\r\nHost: t\r\n\r\n"
                    % quote(seg, safe="").encode()
                )
                await writer.drain()
                raw = await read_response(reader)
                assert raw.split(b"\r\n\r\n", 1)[1].decode() == seg
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(run())
