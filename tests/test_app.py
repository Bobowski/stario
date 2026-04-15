"""Tests for app-level routing and host dispatch."""

import asyncio
from collections.abc import Awaitable, Coroutine
from io import StringIO
from typing import Any, cast

from stario.http.app import App
from stario.http.context import Context
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request
from stario.http.router import Router
from stario.http.writer import Writer
from stario.telemetry import JsonTracer


class DummyWriter:
    def __init__(self) -> None:
        self.status: int | None = None
        self.body: str | None = None
        self.headers = Headers()
        self.started = False
        self._status_code: int | None = None

    @property
    def status_code(self) -> int | None:
        return self._status_code

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        self.body = body.decode("utf-8")
        self.status = status
        self.started = True
        self._status_code = status
        self.headers.set("content-type", content_type.decode("latin-1"))

    def write_headers(self, status: int):
        self.status = status
        self.started = True
        self._status_code = status
        return self

    def end(self, data: bytes | None = None) -> None:
        if data is not None:
            self.body = data.decode("utf-8")
        return None


def _make_request(path: str, *, host: str = "", method: str = "GET") -> Request:
    headers = Headers()
    if host:
        headers.set("host", host)

    reader = BodyReader(
        pause=lambda: None,
        resume=lambda: None,
        disconnect=None,
    )
    reader._cached = b""
    reader._complete = True

    return Request(
        method=method,
        path=path,
        query_bytes=b"",
        headers=headers,
        body=reader,
    )


def make_context(app: App, path: str, *, host: str = "", method: str = "GET") -> Context:
    tracer = JsonTracer(StringIO())
    return Context(
        app=app,
        req=_make_request(path, host=host, method=method),
        span=tracer.create("request"),
        state={},
    )


def _run(awaitable: Awaitable[None]) -> None:
    asyncio.run(cast(Coroutine[Any, Any, None], awaitable))


class TestAppPassthrough:
    def test_app_is_a_router(self):
        assert isinstance(App(), Router)

    def test_registers_routes_via_app(self):
        seen: list[tuple[str, dict[str, str]]] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append((c.route.pattern, dict(c.route.params)))

        app = App()
        app.get("/users/{user_id}", handler, name="user")
        context = make_context(app, "/users/42")

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "/users/{user_id}"
        assert dict(context.route.params) == {"user_id": "42"}
        assert app.url_for("user", params={"user_id": "42"}) == "/users/42"
        assert seen == [("/users/{user_id}", {"user_id": "42"})]

    def test_mounts_router_via_app(self):
        api = Router()
        api.get("/users", async_noop)

        app = App()
        app.mount("/api", api)
        context = make_context(app, "/api/users")

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "/api/users"

    def test_mounts_stario_via_app(self):
        child = App()
        child.get("/users", async_noop)

        app = App()
        app.mount("/api", child)
        context = make_context(app, "/api/users")

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "/api/users"


class TestHostRouting:
    def test_exact_host_routes_to_matching_router(self):
        seen: list[str] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append(c.route.pattern)

        api = Router()
        api.get("/users", handler)

        app = App()
        app.mount("api.example.com/", api)
        context = make_context(app, "/users", host="api.example.com")

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "api.example.com/users"
        assert seen == ["api.example.com/users"]

    def test_wildcard_host_uses_route_params(self):
        seen: list[tuple[str, str]] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append((c.route.params["subhost"], c.route.pattern))

        tenant = Router()
        tenant.get("/dashboard", handler)

        app = App()
        app.mount("{subhost}.example.com/", tenant)
        context = make_context(app, "/dashboard", host="acme.example.com")

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert dict(context.route.params) == {"subhost": "acme"}
        assert context.route.pattern == "{subhost}.example.com/dashboard"
        assert seen == [("acme", "{subhost}.example.com/dashboard")]

    def test_unmatched_host_falls_back_to_app_routes(self):
        seen: list[str] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append(c.route.pattern)

        app = App()
        app.get("/health", handler)
        context = make_context(app, "/health", host="unknown.example.com")

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "/health"
        assert seen == ["/health"]


async def async_noop(c: Context, w: Writer) -> None:
    return None
