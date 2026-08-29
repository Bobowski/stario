"""Tests for app-level routing and host dispatch."""

import logging

import pytest

from stario.exceptions import (
    HttpException,
    StarioError,
)
from stario.http.app import App
from stario.http.context import Context
from stario.http.writer import Writer
from stario.routing import UrlPath
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


class TestAppErrorSurface:
    def test_unhandled_exception_is_raised_and_aborts(self):
        async def boom(_c: Context, _w: Writer) -> None:
            raise RuntimeError("boom")

        def setup(app: App) -> None:
            app.get("/boom", boom)

        with pytest.raises(RuntimeError, match="boom"):
            run_with_app(setup, "/boom")

    def test_handler_must_send_explicit_response(self):
        async def missing_response(_c: Context, _w: Writer) -> None:
            return None

        def setup(app: App) -> None:
            app.get("/missing", missing_response)

        _context, writer = run_with_app(setup, "/missing")

        assert writer.completed
        assert writer.status is None
        assert not writer.ended

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

    def test_http_exception_is_not_mapped_to_http(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise HttpException(422, "nope")

        def setup(app: App) -> None:
            app.get("/x", handler)

        with pytest.raises(HttpException, match="nope"):
            run_with_app(setup, "/x")

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
