"""Tests for app-level routing and host dispatch."""

import pytest

from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    RedirectException,
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
    def test_dispatch_unhandled_exception_returns_500_and_ends(self):
        async def boom(_c: Context, _w: Writer) -> None:
            raise RuntimeError("boom")

        def setup(app: App) -> None:
            app.get("/boom", boom)

        _context, writer = run_with_app(setup, "/boom")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"
        assert writer.ended

    def test_handler_must_send_explicit_response(self):
        async def missing_response(_c: Context, _w: Writer) -> None:
            return None

        def setup(app: App) -> None:
            app.get("/missing", missing_response)

        _context, writer = run_with_app(setup, "/missing")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"
        assert writer.ended

    def test_default_http_exception_handler(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise HttpException(422, "nope")

        def setup(app: App) -> None:
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 422
        assert writer.body == "nope"

    def test_default_redirect_exception_handler(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RedirectException(303, "/next")

        def setup(app: App) -> None:
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 303
        assert writer.headers.get("location") == "/next"
        assert writer.body == ""

    def test_default_client_disconnected_handler_aborts_without_body(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise ClientDisconnected()

        def setup(app: App) -> None:
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status is None
        assert writer.body is None
        assert writer.completed

    def test_unsafe_redirect_exception_falls_back_to_500(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RedirectException(302, "javascript:alert(1)")

        def setup(app: App) -> None:
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"

    def test_unregistered_exception_is_500(self):
        class MyValueError(ValueError):
            pass

        async def handler(_c: Context, _w: Writer) -> None:
            raise MyValueError("subtype")

        def setup(app: App) -> None:
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"
        assert not hasattr(App, "on_error")

    def test_http_exception_subclass_is_still_typed(self):
        class Gone(HttpException):
            def __init__(self) -> None:
                super().__init__(410, "gone")

        async def handler(_c: Context, _w: Writer) -> None:
            raise Gone()

        def setup(app: App) -> None:
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 410
        assert writer.body == "gone"

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
