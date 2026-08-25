"""Parity and behavior tests for the Cython Router and App."""

import asyncio

import pytest

from stario.exceptions import (
    HttpException,
    StarioError,
    StarioRuntime,
)
from stario.http.context import EMPTY_ROUTE_MATCH, Context
from stario.http.dispatch import (
    Router as PyRouter,
    default_not_found as py_default_not_found,
)
from stario.http.writer import Writer
from stario.routing import Route, UrlPath
from stario_cython.app import App
from stario_cython.router import (
    Router,
    default_not_found,
    method_not_allowed_handler,
)
from tests.helpers import DummyWriter, make_context


async def noop_handler(c: Context, w: Writer) -> None:
    return None


def _register(router):
    router.get("/plaintext", noop_handler)
    router.get("/json", noop_handler)
    router.get("/user/{user_id}", noop_handler)
    router.get("/users/{user_id}/posts/{post_id}", noop_handler)
    router.get("/files/{path...}", noop_handler)
    router.post("/echo", noop_handler)
    router.get(UrlPath("/users", host="api.example.com"), noop_handler)
    router.get(UrlPath("/dashboard", host="{subhost}.example.com"), noop_handler)
    router.get(UrlPath("/x", host="{tenant...}.cdn.example.com"), noop_handler)
    router.post("/api", noop_handler)
    router.get("/health", noop_handler)


CASES = [
    ("", "/plaintext", "GET"),
    ("", "/json", "GET"),
    ("", "/user/42", "GET"),
    ("", "/users/42/posts/7", "GET"),
    ("", "/files/docs/readme.txt", "GET"),
    ("", "/missing", "GET"),
    ("", "/plaintext", "POST"),
    ("", "/echo", "POST"),
    ("", "/echo", "GET"),
    ("api.example.com", "/users", "GET"),
    ("api.example.com", "/users", "POST"),
    ("www.example.org", "/health", "GET"),
    ("acme.example.com", "/dashboard", "GET"),
    ("a.b.cdn.example.com", "/x", "GET"),
    ("api.example.com", "/api", "POST"),
    ("", "/", "GET"),
]


def test_find_handler_parity_with_python_router():
    py = PyRouter()
    cy = Router()
    _register(py)
    _register(cy)

    for host, path, method in CASES:
        py_handler, py_match = py.find_handler(host, path, method)
        cy_handler, cy_match = cy.find_handler(host, path, method)
        assert py_match.pattern == cy_match.pattern, (host, path, method)
        assert dict(py_match.params) == dict(cy_match.params), (host, path, method)
        if py_handler is py_default_not_found:
            assert cy_handler is default_not_found
        elif py_match is EMPTY_ROUTE_MATCH and py_handler is not py_default_not_found:
            assert cy_match is EMPTY_ROUTE_MATCH
            assert cy_handler is not default_not_found
        else:
            assert py_handler is not None
            assert cy_handler is not None


def test_matches_path_params():
    router = Router()
    router.get("/users/{user_id}/posts/{post_id}", noop_handler)
    _, match = router.find_handler("", "/users/42/posts/7", "GET")
    assert match.pattern == "/users/{user_id}/posts/{post_id}"
    assert dict(match.params) == {"user_id": "42", "post_id": "7"}


def test_matches_catchall_params():
    router = Router()
    router.get("/files/{path...}", noop_handler)
    _, match = router.find_handler("", "/files/docs/readme.txt", "GET")
    assert match.pattern == "/files/{path...}"
    assert dict(match.params) == {"path": "docs/readme.txt"}


def test_method_not_allowed_allow_header_sorted():
    router = Router()
    router.get("/r", noop_handler)
    router.post("/r", noop_handler)
    router.patch("/r", noop_handler)
    handler, _ = router.find_handler("", "/r", "DELETE")
    assert handler is method_not_allowed_handler(frozenset({"GET", "PATCH", "POST"}))


def test_hostless_fallback_and_host_params():
    router = Router()
    router.get("/health", noop_handler)
    router.get(UrlPath("/dashboard", host="{subhost}.example.com"), noop_handler)
    router.get(UrlPath("/x", host="{tenant...}.cdn.example.com"), noop_handler)

    _, health = router.find_handler("www.example.org", "/health", "GET")
    assert health.pattern == "/health"

    _, dash = router.find_handler("acme.example.com", "/dashboard", "GET")
    assert dict(dash.params) == {"subhost": "acme"}
    assert dash.pattern == "{subhost}.example.com/dashboard"

    _, tenant = router.find_handler("a.b.cdn.example.com", "/x", "GET")
    assert dict(tenant.params) == {"tenant": "a.b"}
    assert tenant.pattern == "{tenant...}.cdn.example.com/x"


def test_add_registers_route():
    router = Router()
    router.add(Route.post("/rooms/{room_id}/send"), noop_handler)
    _, match = router.find_handler("", "/rooms/7/send", "POST")
    assert match.pattern == "/rooms/{room_id}/send"
    assert dict(match.params) == {"room_id": "7"}


def test_rejects_duplicate_route():
    router = Router()
    router.get("/hello", noop_handler)
    with pytest.raises(StarioError, match="Route already registered"):
        router.get("/hello", noop_handler)


def test_cache_hit_returns_same_route_match():
    router = Router()
    router.get("/user/{user_id}", noop_handler)
    _, first = router.find_handler("", "/user/42", "GET")
    _, second = router.find_handler("", "/user/42", "GET")
    assert first is second
    assert dict(first.params) == {"user_id": "42"}


def test_use_applies_middleware():
    calls: list[str] = []

    def scope_middleware(handler):
        async def wrapped(c, w):
            calls.append("scope")
            await handler(c, w)

        return wrapped

    router = Router()
    router.use("/", scope_middleware)
    router.get("/users", noop_handler)
    handler, _ = router.find_handler("", "/users", "GET")

    async def _run():
        await handler(make_context(loop=asyncio.get_running_loop()), DummyWriter())

    asyncio.run(_run())
    assert calls == ["scope"]


def _run_app(setup, path, method="GET", host="", query=None):
    async def _run():
        app = App()
        setup(app)
        loop = asyncio.get_running_loop()
        ctx = make_context(path, method, host, app=app, query=query, loop=loop)
        w = DummyWriter()
        await app(ctx, w)
        return ctx, w

    return asyncio.run(_run())


def test_app_trailing_slash_redirect_preserves_query():
    _ctx, writer = _run_app(lambda _app: None, "/search/", query={"q": "cats", "page": 2})
    assert writer.status == 308
    assert writer.headers.unsafe_get(b"location") == b"/search?q=cats&page=2"


def test_app_params_and_http_exception():
    seen: list[str] = []

    async def handler(c, w):
        seen.append(c.route.params["user_id"])
        raise HttpException(422, "nope")

    _ctx, writer = _run_app(lambda app: app.get("/user/{user_id}", handler), "/user/42")
    assert seen == ["42"]
    assert writer.status == 422
    assert writer.body == "nope"


def test_app_create_task_eager_start():
    async def _run():
        app = App()
        started = False

        async def worker():
            nonlocal started
            started = True
            return 7

        task = app.create_task(worker(), eager_start=True)
        assert started
        assert task.done()
        assert task.result() == 7
        assert task not in app.tasks

    asyncio.run(_run())


def test_app_handler_must_send_response():
    async def missing(_c, _w):
        return None

    async def runtime_error_handler(_c, w, exc):
        assert type(exc) is StarioRuntime
        w.respond(b"missing", b"text/plain", 500)

    def setup(app):
        app.on_error(StarioRuntime, runtime_error_handler)
        app.get("/missing", missing)

    _ctx, writer = _run_app(setup, "/missing")
    assert writer.status == 500
    assert writer.body == "missing"


def test_app_requires_running_loop():
    with pytest.raises(StarioError, match="requires a running event loop"):
        App()
