"""Tests for the public test client and low-level writer fallbacks."""

import asyncio
import json
import zlib
from compression import zstd

import brotli
import pytest

import stario.cookies as cookies
import stario.responses as responses
from stario import App
from stario import datastar as ds
from stario.exceptions import StarioError, StarioRuntime
from stario.html import H1
from stario.http.writer import CompressionConfig, Writer
from stario.testing import TestClient


def _make_app() -> App:
    app = App()

    async def text_route(c, w):
        responses.text(w, "Hello, World!")

    async def html_route(c, w):
        responses.html(w, H1("Hello"))

    async def json_route(c, w):
        responses.json(w, {"message": "hello"})

    async def redirect_route(c, w):
        responses.redirect(w, "/final", 302)

    async def final_route(c, w):
        responses.text(w, "redirected")

    async def redirect_cookie_route(c, w):
        cookies.set_cookie(w, "session", "fresh")
        responses.redirect(w, "/redirect-cookie/final", 302)

    async def redirect_cookie_final(c, w):
        responses.text(w, cookies.get_cookie(c.req, "session") or "missing")

    async def query_route(c, w):
        responses.json(
            w,
            {
                "page": c.req.query.get("page"),
                "tags": c.req.query.getlist("tag"),
            }
        )

    async def json_echo(c, w):
        responses.json(w, json.loads(await c.req.body()))

    async def form_echo(c, w):
        responses.json(
            w,
            {
                "body": (await c.req.body()).decode(),
                "content_type": c.req.headers.get("content-type"),
            }
        )

    async def upload_echo(c, w):
        responses.text(w, (await c.req.body()).decode("utf-8", errors="replace"))

    async def login(c, w):
        cookies.set_cookie(w, "session", "abc123", httponly=True)
        cookies.set_cookie(w, "theme", "dark")
        responses.empty(w)

    async def me(c, w):
        responses.json(
            w,
            {
                "session": cookies.get_cookie(c.req, "session"),
                "theme": cookies.get_cookie(c.req, "theme"),
            }
        )

    async def telemetry_route(c, w):
        c.span.event("handler.hit", {"path": c.req.path})
        responses.json(w, {"ok": True})

    async def compressed_route(c, w):
        responses.text(w, "x" * 2048)

    async def error_route(c, w):
        responses.text(w, "teapot", 418)

    app.get("/", text_route)
    app.get("/html", html_route)
    app.get("/json", json_route)
    app.get("/redirect", redirect_route)
    app.get("/final", final_route)
    app.get("/redirect-cookie", redirect_cookie_route)
    app.get("/redirect-cookie/final", redirect_cookie_final)
    app.get("/query", query_route)
    app.post("/json-echo", json_echo)
    app.post("/form-echo", form_echo)
    app.post("/upload", upload_echo)
    app.get("/login", login)
    app.get("/me", me)
    app.get("/telemetry", telemetry_route)
    app.get("/compressed", compressed_route)
    app.get("/error", error_route)
    return app


def _make_writer() -> tuple[Writer, bytearray, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    sink = bytearray()
    writer = Writer(
        transport_write=sink.extend,
        get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
        on_completed=lambda: None,
        disconnect=loop.create_future(),
        shutdown=loop.create_future(),
    )
    return writer, sink, loop


def _split_response(raw: bytes) -> tuple[bytes, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


def _decode_chunked(body: bytes) -> bytes:
    remaining = body
    decoded = bytearray()
    while remaining:
        size_line, _, rest = remaining.partition(b"\r\n")
        if not rest:
            break
        size = int(size_line.split(b";", 1)[0], 16)
        if size == 0:
            break
        decoded.extend(rest[:size])
        remaining = rest[size + 2 :]
    return bytes(decoded)


@pytest.mark.asyncio
class TestClientBasics:
    async def test_sync_client_handles_text_html_and_json(self):
        async with TestClient(_make_app()) as client:
            text_response = await client.get("/")
            html_response = await client.get("/html")
            json_response = await client.get("/json")

        assert text_response.status_code == 200
        assert text_response.text == "Hello, World!"
        assert text_response.headers.get("content-type") == "text/plain; charset=utf-8"

        assert html_response.status_code == 200
        assert html_response.text == "<h1>Hello</h1>"
        assert html_response.headers.get("content-type") == "text/html; charset=utf-8"

        assert json_response.status_code == 200
        assert json_response.json() == {"message": "hello"}
        assert (
            json_response.headers.get("content-type")
            == "application/json; charset=utf-8"
        )

    async def test_client_follows_redirects_by_default(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/redirect")

        assert response.status_code == 200
        assert response.text == "redirected"
        assert response.url == "http://testserver/final"
        assert len(response.history) == 1
        assert response.history[0].status_code == 302
        assert response.history[0].headers.get("location") == "/final"

    async def test_client_can_disable_redirect_following(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/redirect", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers.get("location") == "/final"

    async def test_redirects_merge_response_cookies_into_request_cookie_overrides(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/redirect-cookie", cookies={"session": "stale"})

        assert response.status_code == 200
        assert response.text == "fresh"

    async def test_client_sends_query_json_form_and_file_payloads(self):
        async with TestClient(_make_app()) as client:
            query_response = await client.get(
                "/query",
                params={"page": 2, "tag": ["a", "b"]},
            )
            json_response = await client.post("/json-echo", json={"name": "Ada"})
            form_response = await client.post(
                "/form-echo", data={"active": True, "count": 2}
            )
            upload_response = await client.post(
                "/upload",
                data={"kind": "avatar"},
                files={"file": ("avatar.txt", "hello", "text/plain")},
            )

        assert query_response.json() == {"page": "2", "tags": ["a", "b"]}
        assert json_response.json() == {"name": "Ada"}
        assert form_response.json() == {
            "body": "active=true&count=2",
            "content_type": "application/x-www-form-urlencoded",
        }
        assert 'name="kind"' in upload_response.text
        assert 'name="file"; filename="avatar.txt"' in upload_response.text
        assert "Content-Type: text/plain" in upload_response.text
        assert "hello" in upload_response.text

    async def test_client_persists_cookies_across_requests(self):
        async with TestClient(_make_app()) as client:
            login_response = await client.get("/login")
            profile_response = await client.get("/me")

        assert login_response.status_code == 204
        assert login_response.cookies == {"session": "abc123", "theme": "dark"}
        assert profile_response.json() == {"session": "abc123", "theme": "dark"}

    async def test_client_records_request_exchange_and_telemetry(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/telemetry")

        rid = response.span_id
        root = client.tracer.get_span(rid)
        assert root is not None
        assert root.attributes["request.method"] == "GET"
        assert root.attributes["request.path"] == "/telemetry"
        assert root.attributes["response.status_code"] == 200
        ev = client.tracer.get_event(rid, "handler.hit")
        assert ev is not None
        assert ev.attributes == {"path": "/telemetry"}

        exchange = client.exchanges[-1]
        assert exchange.request.path == "/telemetry"
        assert exchange.request.url == "http://testserver/telemetry"
        assert exchange.response is response

    async def test_client_exit_drains_app_tasks(self):
        app = App()
        state = {"n": 0}

        async def handler(c, w):
            async def bg():
                await asyncio.sleep(0.02)
                state["n"] = 1

            c.app.create_task(bg())
            responses.text(w, "ok")

        app.get("/", handler)
        async with TestClient(app) as client:
            await client.get("/")
            assert state["n"] == 0
        assert state["n"] == 1

    async def test_drain_tasks_flushes_before_client_exit(self):
        app = App()
        state = {"n": 0}

        async def handler(c, w):
            async def bg():
                await asyncio.sleep(0.02)
                state["n"] = 1

            c.app.create_task(bg())
            responses.text(w, "ok")

        app.get("/", handler)
        async with TestClient(app) as client:
            await client.get("/")
            assert state["n"] == 0
            await client.drain_tasks()
            assert state["n"] == 1

    async def test_client_decompresses_compressed_responses(self):
        compression = CompressionConfig(
            min_size=1,
            zstd_level=-1,
            brotli_level=-1,
            gzip_level=6,
        )
        async with TestClient(_make_app(), compression=compression) as client:
            response = await client.get(
                "/compressed",
                headers={"Accept-Encoding": "gzip"},
            )

        assert response.headers.get("content-encoding") == "gzip"
        assert response.text == "x" * 2048

    async def test_client_prefers_brotli_when_multiple_codecs_are_available(self):
        compression = CompressionConfig(min_size=1)
        async with TestClient(_make_app(), compression=compression) as client:
            response = await client.get(
                "/compressed",
                headers={"Accept-Encoding": "gzip, zstd, br"},
            )

        assert response.headers.get("content-encoding") == "br"
        assert response.text == "x" * 2048

    async def test_client_respects_accept_encoding_qvalues(self):
        compression = CompressionConfig(min_size=1)
        async with TestClient(_make_app(), compression=compression) as client:
            response = await client.get(
                "/compressed",
                headers={"Accept-Encoding": "gzip;q=0.9, br;q=0.2"},
            )

        assert response.headers.get("content-encoding") == "gzip"
        assert response.text == "x" * 2048

    async def test_response_raise_for_status(self):
        async with TestClient(_make_app()) as client:
            response = await client.get("/error")

        with pytest.raises(RuntimeError, match="418 I'm a Teapot: teapot"):
            response.raise_for_status()

    async def test_stream_iter_bytes_chunked(self):
        app = App()

        async def handler(c, w):
            w.write_headers(200)
            w.write(b"hel")
            w.write(b"lo")
            w.end()

        app.get("/s", handler)
        async with TestClient(app) as client:
            async with client.stream(
                "GET", "/s", headers={"Accept-Encoding": "identity"}
            ) as r:
                assert r.status_code == 200
                parts = [c async for c in r.iter_bytes()]
        assert b"".join(parts) == b"hello"

    async def test_stream_body_reads_full_entity(self):
        app = App()

        async def handler(c, w):
            w.write_headers(200)
            w.write(b"hel")
            w.write(b"lo")
            w.end()

        app.get("/s", handler)
        async with TestClient(app) as client:
            async with client.stream(
                "GET", "/s", headers={"Accept-Encoding": "identity"}
            ) as r:
                assert r.status_code == 200
                assert await r.body() == b"hello"

    async def test_stream_iter_events(self):
        app = App()

        async def handler(c, w):
            w.headers.set("content-type", "text/event-stream")
            w.write_headers(200)
            w.write(b"event: ping\ndata: hello\n\n")
            w.end()

        app.get("/e", handler)
        async with TestClient(app) as client:
            async with client.stream(
                "GET", "/e", headers={"Accept-Encoding": "identity"}
            ) as r:
                evs = [e async for e in r.iter_events()]
        assert evs == [{"event": "ping", "data": "hello"}]


class TestTestClient:
    async def test_client_supports_async_tests(self):
        async with TestClient(_make_app()) as client:
            response = await client.post("/json-echo", json={"count": 42})

        assert response.status_code == 200
        assert response.json() == {"count": 42}


class TestWriterRaw:
    def test_patch_writes_namespace_sse_line(self):
        w, sink, loop = _make_writer()
        try:
            ds.sse.patch_elements(
                w,
                b"<circle cx='10' cy='10' r='5'></circle>",
                namespace="svg",
            )

            head, body = _split_response(bytes(sink))
            decoded = _decode_chunked(body)

            assert b"content-type: text/event-stream" in head
            assert b"data: namespace svg" in decoded
            assert b"data: elements <circle cx='10' cy='10' r='5'></circle>" in decoded
        finally:
            loop.close()

    def test_end_empty_bytes_sends_200(self):
        w, sink, loop = _make_writer()
        try:
            w.end(b"")
            assert bytes(sink).startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"content-length: 0\r\n" in bytes(sink)
        finally:
            loop.close()

    def test_end_without_data_sends_204(self):
        w, sink, loop = _make_writer()
        try:
            w.end()
            assert bytes(sink).startswith(b"HTTP/1.1 204 No Content\r\n")
            assert b"content-length: 0\r\n" in bytes(sink)
        finally:
            loop.close()

    def test_redirect_keeps_safe_relative_target(self):
        w, sink, loop = _make_writer()
        try:
            responses.redirect(
                w,
                "/dashboard with spaces?tab=team settings#profile section",
                303,
            )
            head, body = _split_response(bytes(sink))

            assert b"HTTP/1.1 303 See Other\r\n" in head
            assert (
                b"location: /dashboard%20with%20spaces?tab=team%20settings#profile%20section\r\n"
                in head
            )
            assert body == b""
        finally:
            loop.close()

    @pytest.mark.parametrize(
        "target",
        [
            "//evil.example/login",
            r"/\\evil",
            "/safe\r\nx-header: injected",
        ],
    )
    def test_redirect_rejects_unsafe_relative_targets(self, target):
        w, sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="safe app-relative path|control characters"):
                responses.redirect(w, target, 302)
        finally:
            loop.close()

    def test_sse_rejects_non_event_stream_after_headers_started(self):
        w, sink, loop = _make_writer()
        try:
            w.headers.set("content-type", "text/html")
            w.write_headers(200)
            with pytest.raises(StarioRuntime, match="text/event-stream"):
                ds.sse.patch_signals(w, {"x": 1})
        finally:
            loop.close()

    def test_sse_redirect_uses_safe_relative_target(self):
        w, sink, loop = _make_writer()
        try:
            ds.sse.redirect(w, "/home page?tab=recent items#hero banner")
            head, body = _split_response(bytes(sink))
            decoded = _decode_chunked(body)

            assert b"content-type: text/event-stream" in head
            assert (
                b'window.location = "/home%20page?tab=recent%20items#hero%20banner"'
                in decoded
            )
        finally:
            loop.close()

    def test_redirect_allows_relative_targets_with_absolute_urls_in_query(self):
        w, sink, loop = _make_writer()
        try:
            responses.redirect(
                w,
                "/login?next=https://example.com/docs page#section",
                302,
            )
            head, _ = _split_response(bytes(sink))

            assert b"HTTP/1.1 302 Found\r\n" in head
            assert (
                b"location: /login?next=https://example.com/docs%20page#section\r\n"
                in head
            )
        finally:
            loop.close()

    def test_redirect_allows_absolute_targets(self):
        w, sink, loop = _make_writer()
        try:
            responses.redirect(w, "https://example.com/login?next=%2Fw", 302)
            head, _ = _split_response(bytes(sink))

            assert b"HTTP/1.1 302 Found\r\n" in head
            assert b"location: https://example.com/login?next=%2Fw\r\n" in head
        finally:
            loop.close()

    @pytest.mark.parametrize(
        ("accept_encoding", "decompress"),
        [
            ("br", brotli.decompress),
            ("gzip", lambda body: zlib.decompress(body, wbits=31)),
            ("zstd", zstd.decompress),
        ],
    )
    def test_streaming_compression_finishes_frames(
        self,
        accept_encoding: str,
        decompress,
    ):
        compression = CompressionConfig(min_size=1)
        loop = asyncio.new_event_loop()
        sink = bytearray()
        writer = Writer(
            transport_write=sink.extend,
            get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
            on_completed=lambda: None,
            disconnect=loop.create_future(),
            shutdown=loop.create_future(),
            compression=compression,
            accept_encoding=accept_encoding,
        )

        try:
            payload = b"event: patch\ndata: <div>hello</div>\n\n"
            writer.write(payload)
            writer.write(payload)
            writer.end()

            head, body = _split_response(bytes(sink))
            assert b"transfer-encoding: chunked" in head
            compressed = _decode_chunked(body)
            assert decompress(compressed) == payload * 2
        finally:
            loop.close()

    def test_respond_skips_compression_for_image_content_types(self):
        compression = CompressionConfig(min_size=1)
        loop = asyncio.new_event_loop()
        sink = bytearray()
        writer = Writer(
            transport_write=sink.extend,
            get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
            on_completed=lambda: None,
            disconnect=loop.create_future(),
            shutdown=loop.create_future(),
            compression=compression,
            accept_encoding="gzip",
        )

        try:
            payload = b"\x89PNG\r\n\x1a\n" + b"x" * 2048
            writer.respond(payload, b"image/png", 200)

            head, body = _split_response(bytes(sink))
            assert b"content-encoding:" not in head
            assert body == payload
        finally:
            loop.close()

    def test_chunked_writes_skip_compression_for_image_content_types(self):
        compression = CompressionConfig(min_size=1)
        loop = asyncio.new_event_loop()
        sink = bytearray()
        writer = Writer(
            transport_write=sink.extend,
            get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
            on_completed=lambda: None,
            disconnect=loop.create_future(),
            shutdown=loop.create_future(),
            compression=compression,
            accept_encoding="gzip",
        )

        try:
            payload = b"\x89PNG\r\n\x1a\n" + b"x" * 256
            writer.headers.rset(b"content-type", b"image/png")
            writer.write(payload)
            writer.end()

            head, body = _split_response(bytes(sink))
            assert b"content-encoding:" not in head
            assert _decode_chunked(body) == payload
        finally:
            loop.close()

    def test_select_skips_encodings_with_zero_qvalue(self):
        compression = CompressionConfig(min_size=1)

        compressor = compression.select("br;q=0, gzip;q=0.5")

        assert compressor is not None
        assert compressor.encoding == b"gzip"

    def test_select_does_not_match_substrings(self):
        compression = CompressionConfig(min_size=1)

        assert compression.select("abracadabra") is None

    def test_select_returns_none_immediately_when_all_codecs_disabled(self):
        compression = CompressionConfig(
            zstd_level=-1,
            brotli_level=-1,
            gzip_level=-1,
        )
        assert (
            compression.select(
                "gzip, deflate, br, zstd",
                data=b"ok",
                content_type=b"text/plain",
            )
            is None
        )
