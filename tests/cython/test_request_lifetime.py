"""c / w / c.req lifetime: nothing a handler got may observe another request."""

from __future__ import annotations

import asyncio

import pytest

import stario.responses as responses
from stario import App, Route
from stario.exceptions import StarioRuntime
from stario_cython.exchange import _retained_detach_count
from tests.cython.http import read_response, running_server


def _body(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[1]


@pytest.mark.asyncio
async def test_retained_request_keeps_its_own_data_after_keep_alive_reuse() -> None:
    app = App()
    kept = []

    async def keep(c, w) -> None:
        kept.append(c.req)
        responses.text(w, "kept")

    async def other(c, w) -> None:
        await c.req.body()
        responses.text(w, "other")

    app.add(Route("GET /keep/{who}"), keep)
    app.add(Route("POST /other"), other)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /keep/al%20ice?who=alice HTTP/1.1\r\nHost: Alice.Test\r\n"
            b"X-Secret: alice-token\r\nCookie: sid=alice\r\n\r\n"
        )
        assert _body(await read_response(reader)) == b"kept"
        writer.write(
            b"POST /other?who=bob HTTP/1.1\r\nHost: bob.test\r\n"
            b"X-Secret: bob-token\r\nCookie: sid=bob\r\nContent-Length: 3\r\n\r\nbob"
        )
        assert _body(await read_response(reader)) == b"other"
        writer.close()

    req = kept[0]
    assert req.headers.get("x-secret") == "alice-token"
    assert req.host == "alice.test"
    assert req.query_bytes == b"who=alice"
    assert req.query.get("who") == "alice"
    assert req.cookies.get("sid") == "alice"
    assert req.path == "/keep/al ice"
    assert req.raw_path == b"/keep/al%20ice"


@pytest.mark.asyncio
async def test_retained_request_views_survive_a_freed_arena() -> None:
    """Large arenas are freed on recycle; retained views must own a copy."""
    app = App()
    kept = []

    async def keep(c, w) -> None:
        kept.append((c.req.headers, c.req.query))
        responses.text(w, "ok")

    app.add(Route("GET /keep"), keep)
    query = b"q=" + b"z" * 9000
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /keep?"
            + query
            + b" HTTP/1.1\r\nHost: a\r\nX-Big: "
            + b"b" * 9000
            + b"\r\nConnection: close\r\n\r\n"
        )
        await read_response(reader)
        writer.close()
        await asyncio.sleep(0.05)

    headers, parsed_query = kept[0]
    assert parsed_query.get("q") == "z" * 9000
    assert headers.get("x-big") == "b" * 9000


@pytest.mark.asyncio
async def test_retained_request_body_is_cached_or_gone() -> None:
    app = App()
    kept = []

    async def read_it(c, w) -> None:
        await c.req.body()
        kept.append(c.req)
        responses.text(w, "read")

    async def ignore_it(c, w) -> None:
        kept.append(c.req)
        responses.text(w, "ignored")

    app.add(Route("POST /read"), read_it)
    app.add(Route("POST /ignore"), ignore_it)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /read HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\n\r\nhello"
        )
        await read_response(reader)
        big = 300 * 1024
        writer.write(
            b"POST /ignore HTTP/1.1\r\nHost: a\r\nContent-Length: %d\r\n\r\n" % big
            + b"x" * big
        )
        await read_response(reader)
        writer.write(b"GET /missing HTTP/1.1\r\nHost: a\r\n\r\n")
        await read_response(reader)
        writer.close()

    assert await kept[0].body() == b"hello"
    with pytest.raises(StarioRuntime, match="no longer available"):
        await kept[1].body()


@pytest.mark.asyncio
async def test_finished_writer_raises_and_never_reaches_another_client() -> None:
    app = App()
    outcome: dict[str, object] = {}
    kept = []

    async def alice(c, w) -> None:
        kept.append(w)

        async def late() -> None:
            await asyncio.sleep(0.2)
            try:
                w.respond(b"ALICE-PRIVATE", b"text/plain")
            except StarioRuntime as exc:
                outcome["respond"] = str(exc)
            try:
                w.write(b"more")
            except StarioRuntime:
                outcome["write"] = "raised"
            try:
                w.headers.set("x-late", "1")
            except StarioRuntime:
                outcome["headers"] = "raised"
            w.end()
            w.abort()
            outcome["status"] = w.status_code
            outcome["completed"] = w.completed
            outcome["closing"] = c.closing
            outcome["match"] = c.match.pattern
            outcome["path"] = c.req.path

        app.create_task(late())
        responses.text(w, "alice ok")

    async def bob(_c, w) -> None:
        await asyncio.sleep(0.4)
        responses.text(w, "bob ok")

    app.add(Route("GET /alice"), alice)
    app.add(Route("GET /bob"), bob)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /alice HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\n")
        assert _body(await read_response(reader)) == b"alice ok"
        writer.close()
        await asyncio.sleep(0.05)
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
        writer2.write(b"GET /bob HTTP/1.1\r\nHost: a\r\n\r\n")
        assert _body(await read_response(reader2)) == b"bob ok"
        writer2.close()
        await app.drain_tasks()

    assert "request has finished" in str(outcome["respond"])
    assert outcome["write"] == "raised"
    assert outcome["headers"] == "raised"
    assert outcome["status"] == 200
    assert outcome["completed"] is True
    assert outcome["closing"] is True
    assert outcome["match"] == "GET /alice"
    assert outcome["path"] == "/alice"
    assert "finished" in repr(kept[0])


@pytest.mark.asyncio
async def test_kept_response_headers_do_not_leak_into_the_next_response() -> None:
    app = App()
    kept = []

    async def first(_c, w) -> None:
        kept.append(w.headers)
        responses.text(w, "first")

    async def second(_c, w) -> None:
        kept[0].set("x-leak", "yes")
        responses.text(w, "second")

    app.add(Route("GET /first"), first)
    app.add(Route("GET /second"), second)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /first HTTP/1.1\r\nHost: a\r\n\r\n")
        await read_response(reader)
        writer.write(b"GET /second HTTP/1.1\r\nHost: a\r\n\r\n")
        response = await read_response(reader)
        writer.close()

    assert b"x-leak" not in response.lower()


@pytest.mark.asyncio
async def test_plain_requests_never_take_the_retained_copy_path() -> None:
    """Recycle must see no outside reference unless user code kept one."""
    app = App()

    async def sync(c, w) -> None:
        c.req.headers.get("host")
        c.req.query.get("q")
        c.req.cookies.get("a")
        c.state["x"] = 1
        responses.text(w, "sync")

    async def suspended(c, w) -> None:
        await asyncio.sleep(0)
        w.headers.set("x-kind", "async")
        responses.text(w, c.req.path)

    async def post_respond(c, w) -> None:
        responses.text(w, "early")
        await asyncio.sleep(0.01)
        c.req.headers.get("host")

    async def body(c, w) -> None:
        responses.text(w, (await c.req.body()).decode())

    async def streamed(c, w) -> None:
        w.write_headers(200)
        w.write(b"a")
        await asyncio.sleep(0)
        w.end(b"b")

    for route, handler in (
        ("GET /sync", sync),
        ("GET /async", suspended),
        ("GET /post", post_respond),
        ("POST /body", body),
        ("GET /stream", streamed),
    ):
        app.add(Route(route), handler)

    before = _retained_detach_count()
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for target in (
            b"/sync?q=1",
            b"/async",
            b"/post",
            b"/stream",
            b"/missing",
            b"/sync/",
        ):
            writer.write(
                b"GET " + target + b" HTTP/1.1\r\nHost: a\r\nCookie: a=1\r\n\r\n"
            )
            if target == b"/stream":
                await reader.readuntil(b"0\r\n\r\n")
            else:
                await read_response(reader)
        writer.write(b"POST /body HTTP/1.1\r\nHost: a\r\nContent-Length: 2\r\n\r\nhi")
        await read_response(reader)
        writer.write(
            b"GET /async HTTP/1.1\r\nHost: a\r\n\r\n"
            b"GET /sync HTTP/1.1\r\nHost: a\r\n\r\n"
        )
        await read_response(reader)
        await read_response(reader)
        await asyncio.sleep(0.05)
        writer.close()
        await app.drain_tasks()
    assert _retained_detach_count() == before

    kept = []

    async def keeper(c, w) -> None:
        kept.append(c)
        responses.text(w, "kept")

    app.add(Route("GET /keep"), keeper)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /keep HTTP/1.1\r\nHost: a\r\n\r\n")
        await read_response(reader)
        writer.close()
    assert _retained_detach_count() > before


@pytest.mark.asyncio
async def test_handle_is_per_request_but_exchange_is_reused() -> None:
    app = App()
    handles = []
    exchange_ids = []

    async def endpoint(c, w) -> None:
        assert c is w
        handles.append(c)
        exchange_ids.append(c._exchange_id)
        c.state["n"] = len(handles)
        responses.text(w, "ok")

    app.add(Route("GET /"), endpoint)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(2):
            writer.write(b"GET / HTTP/1.1\r\nHost: a\r\n\r\n")
            await read_response(reader)
        writer.close()

    assert handles[0] is not handles[1]
    assert exchange_ids[0] == exchange_ids[1]
    assert handles[0].state == {"n": 1}
    assert handles[1].state == {"n": 2}


@pytest.mark.asyncio
async def test_h2_streams_never_take_the_retained_copy_path() -> None:
    from tests.cython import h2wire as h2

    app = App()

    async def sync(c, w) -> None:
        c.req.headers.get("host")
        responses.text(w, "sync")

    async def suspended(c, w) -> None:
        await asyncio.sleep(0)
        responses.text(w, c.req.path)

    app.add(Route("GET /sync"), sync)
    app.add(Route("GET /async"), suspended)
    before = _retained_detach_count()
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                1,
                h2.encode_request(path="/sync"),
            )
            + h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                3,
                h2.encode_request(path="/async"),
            )
        )
        frames, buf = await h2.read_stream(reader, buf, 1)
        more: list[h2.H2Frame] = []
        if not h2.stream_ended(frames, 3):
            more, buf = await h2.read_stream(reader, buf, 3)
        writer.close()
        await asyncio.sleep(0.05)
    assert h2.stream_data(frames + more, 1) == b"sync"
    assert h2.stream_data(frames + more, 3) == b"/async"
    assert _retained_detach_count() == before


@pytest.mark.asyncio
async def test_body_stream_outliving_its_request_cannot_read_the_next_body() -> None:
    app = App()
    stale: list[object] = []
    second: list[bytes] = []
    resume = asyncio.Event()
    finished = asyncio.Event()

    async def drain_later(chunks) -> None:
        await resume.wait()
        try:
            async for chunk in chunks:
                stale.append(chunk)
        except StarioRuntime as exc:
            stale.append(type(exc))
        finished.set()

    async def upload(c, w) -> None:
        if not second and not stale:
            chunks = c.req.stream(max_chunk=1).__aiter__()
            stale.append(await chunks.__anext__())
            asyncio.get_running_loop().create_task(drain_later(chunks))
        else:
            second.append(await c.req.body())
        responses.text(w, "ok")

    app.add(Route("POST /"), upload)
    chunked = b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(chunked + b"2\r\nab\r\n")
        await asyncio.sleep(0.05)
        writer.write(b"0\r\n\r\n")
        await read_response(reader)
        writer.write(chunked + b"6\r\nSECRET\r\n")
        await asyncio.sleep(0.05)
        resume.set()
        async with asyncio.timeout(2):
            await finished.wait()
        writer.write(b"0\r\n\r\n")
        await read_response(reader)
        writer.close()
    assert stale == [b"a", StarioRuntime]
    assert second == [b"SECRET"]
