"""Request-target handling: RFC 9112 §3.2 forms and RFC 3986 path rules."""

from __future__ import annotations

import asyncio

import pytest

import stario.responses as responses
from stario import App, Route
from stario.exceptions import StarioError
from tests.cython import h2wire as h2
from tests.cython.http import read_response, running_server


def _status(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def _header(response: bytes, name: bytes) -> bytes | None:
    head = response.split(b"\r\n\r\n", 1)[0]
    for line in head.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        if key.strip().lower() == name:
            return value.strip()
    return None


def _body(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[1]


def _echo_app() -> App:
    app = App()

    async def show(c, w) -> None:
        params = ",".join(f"{k}={v}" for k, v in sorted(c.match.params.items()))
        responses.text(
            w,
            f"{c.req.method} {c.req.path} {c.req.raw_path.decode()} [{params}] {c.req.host}",
        )

    for route in (
        "GET /",
        "OPTIONS /",
        "GET /a",
        "GET /a/{x}",
        "GET /files/{rest...}",
        "GET //api.example.com/x/{id}",
    ):
        app.add(Route(route), show)
    return app


async def _get(port: int, target: bytes, host: bytes = b"t") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"GET " + target + b" HTTP/1.1\r\nHost: " + host + b"\r\n\r\n")
        return await read_response(reader)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_encoded_slash_is_data_inside_one_fully_decoded_segment() -> None:
    async with running_server(_echo_app()) as port:
        assert _body(await _get(port, b"/a/x%2Fy")) == b"GET /a/x/y /a/x%2Fy [x=x/y] t"
        assert (
            _body(await _get(port, b"/a/x%252Fy"))
            == b"GET /a/x%2Fy /a/x%252Fy [x=x%2Fy] t"
        )
        assert (
            _body(await _get(port, b"/%61/%C3%A9"))
            == "GET /a/é /%61/%C3%A9 [x=é] t".encode()
        )
        assert _body(await _get(port, b"/files/a%2Fb/c")) == (
            b"GET /files/a/b/c /files/a%2Fb/c [rest=a/b/c] t"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "location"),
    [
        (b"/a/./x", b"/a/x"),
        (b"/a/b/../x", b"/a/x"),
        (b"/a/%2e%2E/a", b"/a"),
        (b"/files/x/.%2E/a", b"/files/a"),
        (b"/files/.%2E/.%2e/a", b"/a"),
        (b"/a/x/?q=1", b"/a/x?q=1"),
        (b"//evil.test/", b"/evil.test"),
    ],
)
async def test_dot_segments_and_trailing_slash_redirect_to_canonical_path(
    target: bytes, location: bytes
) -> None:
    async with running_server(_echo_app()) as port:
        response = await _get(port, target)
    assert _status(response) == 308
    assert _header(response, b"location") == location


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        b"/a/%0D%0Ax",
        b"/a/%7F",
        b"/a/%00",
        b"/a#frag",
        b"/a?x=1#frag",
        b"/a/%zz",
        b"/a/%C3",
        b"*",
    ],
)
async def test_invalid_targets_answer_400_and_keep_the_connection(
    target: bytes,
) -> None:
    async with running_server(_echo_app()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET " + target + b" HTTP/1.1\r\nHost: t\r\n\r\n")
        response = await read_response(reader)
        assert _status(response) == 400
        writer.write(b"GET /a HTTP/1.1\r\nHost: t\r\n\r\n")
        assert _status(await read_response(reader)) == 200
        writer.close()


@pytest.mark.asyncio
async def test_options_asterisk_is_answered_by_the_server() -> None:
    async with running_server(_echo_app()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"OPTIONS * HTTP/1.1\r\nHost: t\r\n\r\n")
        response = await read_response(reader)
        writer.close()
    assert _status(response) == 204
    assert _header(response, b"content-length") is None
    assert _body(response) == b""


@pytest.mark.asyncio
async def test_absolute_form_routes_on_path_and_uses_uri_authority_as_host() -> None:
    async with running_server(_echo_app()) as port:
        response = await _get(
            port, b"HTTP://API.Example.com:8080/x/7?q=1", host=b"other.test"
        )
        assert _status(response) == 200
        assert _body(response).endswith(b"[id=7] api.example.com")
        assert _body(await _get(port, b"http://t")).startswith(b"GET / / []")
        assert _status(await _get(port, b"http://user@t/a")) == 400
        assert _status(await _get(port, b"ftp://t/a")) == 400


@pytest.mark.asyncio
async def test_bad_target_in_a_pipeline_is_answered_in_order() -> None:
    app = App()

    async def slow(_c, w) -> None:
        await asyncio.sleep(0.05)
        responses.text(w, "slow")

    app.add(Route("GET /slow"), slow)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /%zz HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        statuses = [_status(await read_response(reader)) for _ in range(3)]
        writer.close()
    assert statuses == [200, 400, 200]


@pytest.mark.asyncio
async def test_h2_bad_path_fails_only_its_stream() -> None:
    app = App()

    async def slow(_c, w) -> None:
        await asyncio.sleep(0.2)
        responses.text(w, "slow ok")

    app.add(Route("GET /slow"), slow)
    app.add(Route("GET /a/{x}"), slow)

    def request(stream_id: int, path: bytes) -> bytes:
        block = b"".join(
            [
                h2.indexed(h2.IDX_METHOD_GET),
                h2.indexed(h2.IDX_SCHEME_HTTP),
                h2.literal_name(h2.IDX_PATH_SLASH, path),
                h2.literal_name(h2.IDX_AUTHORITY, b"t"),
            ]
        )
        return h2.pack_frame(
            h2.TYPE_HEADERS, h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM, stream_id, block
        )

    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(request(1, b"/slow"))
        await asyncio.sleep(0.02)
        writer.write(request(3, b"/a/%zz") + request(5, "/a/\u00e9".encode()))
        frames, buf = await h2.read_stream(reader, buf, 1)
        writer.close()
    assert h2.stream_data(frames, 1) == b"slow ok"
    assert b"Invalid HTTP request" in h2.stream_data(frames, 3)
    assert b"Invalid HTTP request" in h2.stream_data(frames, 5)
    assert not h2.has_goaway(frames)


@pytest.mark.asyncio
async def test_no_content_statuses_omit_content_length() -> None:
    app = App()

    async def empty(_c, w) -> None:
        responses.empty(w)

    async def ended(_c, w) -> None:
        w.headers.set("x-kind", "end")
        w.end()

    async def not_modified(_c, w) -> None:
        w.respond(b"", b"text/plain", 304)

    app.add(Route("GET /empty"), empty)
    app.add(Route("GET /end"), ended)
    app.add(Route("GET /304"), not_modified)
    async with running_server(app) as port:
        for target in (b"/empty", b"/end", b"/304"):
            response = await _get(port, target)
            assert _status(response) in (204, 304)
            assert _header(response, b"content-length") is None
            assert _header(response, b"content-type") is None


@pytest.mark.asyncio
async def test_respond_rejects_non_final_status() -> None:
    app = App()
    errors: list[str] = []

    async def informational(_c, w) -> None:
        try:
            w.respond(b"", b"text/plain", 101)
        except StarioError as exc:
            errors.append(str(exc))
        responses.text(w, "ok")

    app.add(Route("GET /"), informational)
    async with running_server(app) as port:
        assert _status(await _get(port, b"/")) == 200
    assert errors
    assert "Invalid response status" in errors[0]


def test_find_handler_takes_the_raw_path() -> None:
    async def run() -> None:
        app = App()

        async def handler(_c, _w) -> None:
            pass

        app.add(Route("GET /files/{name}"), handler)
        app.add(Route("GET /caf\u00e9"), handler)
        _, _, hit = app.find_handler("", "/files/a%2Fb", "GET")
        assert dict(hit.params) == {"name": "a/b"}
        assert app.find_handler("", "/caf%C3%A9", "GET")[2].pattern == "GET /caf\u00e9"
        assert app.find_handler("", "/caf\u00e9", "GET")[2].pattern == "GET /caf\u00e9"
        assert app.find_handler("", "/files/%zz", "GET")[2].pattern == ""

    asyncio.run(run())


@pytest.mark.parametrize("pattern", ["GET /a/./b", "GET /a/.."])
def test_route_patterns_reject_dot_segments(pattern: str) -> None:
    with pytest.raises(StarioError, match="dot segment"):
        Route(pattern)
