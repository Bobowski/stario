"""Tests for app-level routing and host dispatch."""

import logging

import pytest

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.app import App
from stario.http.context import Context
from stario.http.middleware import catch_errors
from stario.http.writer import Writer
from stario.routing import UrlPath
from stario.testing import TestClient
from tests.helpers import run_with_app


def test_eager_create_task_uses_direct_task_with_uvloop() -> None:
    uvloop = pytest.importorskip("uvloop")

    async def run() -> None:
        app = App()

        async def complete_immediately() -> int:
            return 42

        task = app.create_task(complete_immediately(), eager_start=True)

        assert task.done()
        assert task.result() == 42

    uvloop.run(run())


class TestHostRouting:
    def test_wildcard_host_uses_route_params(self):
        seen: list[tuple[str, str]] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append((c.route.params["subhost"], c.route.pattern))
            w.end()

        def setup(app: App) -> None:
            app.get(UrlPath("/dashboard", host="{subhost}.example.com"), handler)

        context, _writer = run_with_app(setup, "/dashboard", host="acme.example.com")

        assert dict(context.route.params) == {"subhost": "acme"}
        assert context.route.pattern == "{subhost}.example.com/dashboard"
        assert seen == [("acme", "{subhost}.example.com/dashboard")]

    def test_trailing_slash_redirect_preserves_query_string(self):
        _context, writer = run_with_app(
            lambda _app: None, "/search/", query={"q": "cats", "page": 2}
        )

        assert writer.status == 308
        assert writer.headers.unsafe_get(b"location") == b"/search?q=cats&page=2"

    @pytest.mark.asyncio
    async def test_trailing_slash_redirect_finishes_span(self):
        app = App()

        async def search(_c: Context, w: Writer) -> None:
            w.respond(b"ok", b"text/plain", 200)

        app.get("/search", search)
        async with TestClient(app) as client:
            response = await client.get("/search/?q=cats", follow_redirects=False)
            assert response.status_code == 308
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.attributes.get("response.status_code") == 308
            assert span.attributes.get("request.method") == "GET"
            assert span.attributes.get("request.path") == "/search/"
            assert span.ok
            assert not client.tracer.has_open_spans()


class TestAppErrorSurface:
    def test_unhandled_exception_writes_500(self, caplog: pytest.LogCaptureFixture):
        async def boom(_c: Context, _w: Writer) -> None:
            raise RuntimeError("boom")

        def setup(app: App) -> None:
            app.get("/boom", boom)

        with caplog.at_level(logging.ERROR, logger="stario.http"):
            _context, writer = run_with_app(setup, "/boom")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"
        assert writer.completed
        assert "Handler failed" in caplog.text

    @pytest.mark.asyncio
    async def test_unhandled_exception_is_500_on_test_client(self):
        app = App()

        async def boom(_c: Context, _w: Writer) -> None:
            raise RuntimeError("boom")

        app.get("/boom", boom)
        async with TestClient(app) as client:
            response = await client.get("/boom")
            assert response.status_code == 500
            assert response.text == "Internal Server Error"
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.attributes.get("response.status_code") == 500
            assert not span.ok
            assert not client.tracer.has_open_spans()

    def test_handler_must_send_explicit_response(self):
        async def missing_response(_c: Context, _w: Writer) -> None:
            return None

        def setup(app: App) -> None:
            app.get("/missing", missing_response)

        _context, writer = run_with_app(setup, "/missing")

        assert writer.completed
        assert writer.status == 500
        assert writer.body == "Internal Server Error"

    def test_write_then_raise_keeps_response_and_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ):
        async def handler(_c: Context, w: Writer) -> None:
            w.respond(b"ok", b"text/plain", 200)
            raise RuntimeError("after write")

        def setup(app: App) -> None:
            app.get("/x", handler)

        with caplog.at_level(logging.ERROR, logger="stario.http"):
            _context, writer = run_with_app(setup, "/x")

        assert writer.status == 200
        assert writer.body == "ok"
        assert writer.completed
        assert "Handler failed" in caplog.text

    def test_uncaught_app_error_becomes_500_without_middleware(self):
        class AppError(Exception):
            pass

        async def handler(_c: Context, _w: Writer) -> None:
            raise AppError("nope")

        def setup(app: App) -> None:
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"

    def test_catch_errors_middleware_maps_app_exception(self):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, exc: BaseException) -> None:
            responses.text(w, str(exc), 422)

        async def handler(_c: Context, _w: Writer) -> None:
            raise AppError("nope")

        def setup(app: App) -> None:
            app.use("/", catch_errors(AppError, respond=respond))
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 422
        assert writer.body == "nope"

    def test_call_uses_find_handler(self):
        seen: list[tuple[str, str, str]] = []

        async def handler(_c: Context, w: Writer) -> None:
            w.respond(b"ok", b"text/plain", 200)

        def setup(app: App) -> None:
            app.get("/x", handler)
            original = app.find_handler

            def wrapped(host: str, path: str, method: str):
                seen.append((host, path, method))
                return original(host, path, method)

            app.find_handler = wrapped  # type: ignore[method-assign]

        _context, writer = run_with_app(setup, "/x")

        assert seen == [("", "/x", "GET")]
        assert writer.status == 200
        assert writer.body == "ok"

    def test_app_requires_running_loop(self):
        with pytest.raises(StarioError, match="requires a running event loop"):
            App()
