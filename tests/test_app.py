"""Tests for app-level routing and host dispatch."""

import pytest

import stario.responses as responses
from stario import Route
from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    RedirectException,
    StarioError,
    StarioRuntime,
)
from stario.http.app import App
from stario.http.context import Context
from stario.http.writer import Writer
from stario.testing import TestClient
from tests.helpers import run_with_app


class TestHostRouting:
    def test_wildcard_host_uses_route_params(self):
        seen: list[tuple[str, str]] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append((c.match.params["subhost"], c.match.pattern))
            w.end()

        def setup(app: App) -> None:
            app.add(
                Route("GET //{subhost}.example.com/dashboard"), handler
            )

        context, _writer = run_with_app(setup, "/dashboard", host="acme.example.com")

        assert dict(context.match.params) == {"subhost": "acme"}
        assert context.match.pattern == "GET //{subhost}.example.com/dashboard"
        assert seen == [("acme", "GET //{subhost}.example.com/dashboard")]

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
            app.add(Route("GET", "/boom"), boom)

        _context, writer = run_with_app(setup, "/boom")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"
        assert writer.ended

    def test_handler_must_send_explicit_response(self):
        seen: list[type[Exception]] = []

        async def missing_response(_c: Context, _w: Writer) -> None:
            return None

        async def runtime_error_handler(
            _c: Context,
            w: Writer,
            exc: Exception,
        ) -> None:
            seen.append(type(exc))
            w.respond(b"missing", b"text/plain", 500)

        def setup(app: App) -> None:
            app.on_error(StarioRuntime, runtime_error_handler)
            app.add(Route("GET", "/missing"), missing_response)

        _context, writer = run_with_app(setup, "/missing")

        assert seen == [StarioRuntime]
        assert writer.status == 500
        assert writer.body == "missing"

    def test_default_http_exception_handler(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise HttpException(422, "nope")

        def setup(app: App) -> None:
            app.add(Route("GET", "/x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 422
        assert writer.body == "nope"

    def test_default_redirect_exception_handler(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RedirectException(303, "/next")

        def setup(app: App) -> None:
            app.add(Route("GET", "/x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 303
        assert writer.headers.get("location") == "/next"
        assert writer.body == ""

    def test_default_client_disconnected_handler_aborts_without_body(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise ClientDisconnected()

        def setup(app: App) -> None:
            app.add(Route("GET", "/x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status is None
        assert writer.body is None
        assert writer.completed

    def test_unsafe_redirect_exception_falls_back_to_500(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RedirectException(302, "javascript:alert(1)")

        def setup(app: App) -> None:
            app.add(Route("GET", "/x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"

    def test_on_error_handles_subclass_via_mro(self):
        async def custom(_c: Context, w: Writer, _exc: Exception) -> None:
            w.respond(b"handled", b"text/plain", 418)

        class MyValueError(ValueError):
            pass

        async def handler(_c: Context, _w: Writer) -> None:
            raise MyValueError("subtype")

        def setup(app: App) -> None:
            app.on_error(ValueError, custom)
            app.add(Route("GET", "/x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 418
        assert writer.body == "handled"

    def test_error_handler_failure_falls_back_to_500(self):
        async def bad_handler(_c: Context, _w: Writer, _exc: Exception) -> None:
            raise RuntimeError("handler failed")

        async def handler(_c: Context, _w: Writer) -> None:
            raise ValueError("original")

        def setup(app: App) -> None:
            app.on_error(ValueError, bad_handler)
            app.add(Route("GET", "/x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 500
        assert writer.body == "Internal Server Error"

    def test_app_requires_running_loop(self):
        with pytest.raises(StarioError, match="requires a running event loop"):
            App()


class TestRequestSpanName:
    async def test_matched_route_renames_span(self):
        async def ok(c: Context, w: Writer) -> None:
            responses.empty(w, 204)

        app = App()
        app.add(Route("GET", "/users/{user_id}"), ok)
        async with TestClient(app) as client:
            response = await client.get("/users/7")

        root = client.tracer.get_span(response.span_id)
        assert root is not None
        assert root.name == "GET /users/{user_id}"
        assert root.attributes["http.route"] == "/users/{user_id}"

    async def test_trailing_slash_uses_canonical_route_name(self):
        async def ok(c: Context, w: Writer) -> None:
            responses.empty(w, 204)

        app = App()
        app.add(Route("GET", "/search"), ok)
        async with TestClient(app) as client:
            response = await client.get("/search/", follow_redirects=False)

        assert response.status_code == 308
        root = client.tracer.get_span(response.span_id)
        assert root is not None
        assert root.name == "GET /search"

    async def test_not_found_keeps_method_name(self):
        async with TestClient(App()) as client:
            response = await client.get("/missing")

        root = client.tracer.get_span(response.span_id)
        assert root is not None
        assert root.name == "GET"
        assert "http.route" not in root.attributes
