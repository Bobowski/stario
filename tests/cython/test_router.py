"""Parity and behavior tests for the Cython Router and App."""

import asyncio

import pytest
from stario_cython.app import App
from stario_cython.router import (
    Router,
    default_not_found,
    method_not_allowed_handler,
)

import stario.responses as responses
from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    RedirectException,
    StarioError,
    StarioRuntime,
)
from stario.http.context import EMPTY_ROUTE_MATCH, Context
from stario.http.dispatch import (
    Router as PyRouter,
    default_not_found as py_default_not_found,
    method_not_allowed_handler as py_method_not_allowed_handler,
)
from stario.http.writer import Writer
from stario.routing import Route, UrlPath
from tests.cython.http import read_response, running_server
from tests.helpers import DummyWriter, make_context


async def noop_handler(c: Context, w: Writer) -> None:
    return None


def _register(router: PyRouter | Router) -> None:
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
    router.get("/foo/POST", noop_handler)


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
    ("", "/foo/POST", "GET"),
    ("", "/foo", "GET"),
    ("API.Example.Com", "/users", "GET"),
]


def test_find_handler_parity_with_python_router() -> None:
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


def test_static_exact_and_wildcard_coexist() -> None:
    router = Router()
    router.get("/users/me", noop_handler)
    router.get("/users/{user_id}", noop_handler)

    _, me = router.find_handler("", "/users/me", "GET")
    _, other = router.find_handler("", "/users/42", "GET")

    assert me.pattern == "/users/me"
    assert dict(me.params) == {}
    assert other.pattern == "/users/{user_id}"
    assert dict(other.params) == {"user_id": "42"}


def test_host_route_wins_over_hostless_exact() -> None:
    async def host_handler(c: Context, w: Writer) -> None:
        return None

    async def path_handler(c: Context, w: Writer) -> None:
        return None

    router = Router()
    router.get("/health", path_handler)
    router.get(UrlPath("/health", host="api.example.com"), host_handler)

    handler, match = router.find_handler("api.example.com", "/health", "GET")
    assert handler is host_handler
    assert match.pattern == "api.example.com/health"

    handler, match = router.find_handler("www.example.org", "/health", "GET")
    assert handler is path_handler
    assert match.pattern == "/health"


def test_matches_path_params() -> None:
    router = Router()
    router.get("/users/{user_id}/posts/{post_id}", noop_handler)
    _, match = router.find_handler("", "/users/42/posts/7", "GET")
    assert match.pattern == "/users/{user_id}/posts/{post_id}"
    assert dict(match.params) == {"user_id": "42", "post_id": "7"}


def test_matches_catchall_params() -> None:
    router = Router()
    router.get("/files/{path...}", noop_handler)
    _, match = router.find_handler("", "/files/docs/readme.txt", "GET")
    assert match.pattern == "/files/{path...}"
    assert dict(match.params) == {"path": "docs/readme.txt"}


def test_method_not_allowed_allow_header_sorted() -> None:
    router = Router()
    router.get("/r", noop_handler)
    router.post("/r", noop_handler)
    router.patch("/r", noop_handler)
    handler, _ = router.find_handler("", "/r", "DELETE")
    assert handler is method_not_allowed_handler(frozenset({"GET", "PATCH", "POST"}))

    async def _run():
        w = DummyWriter()
        await handler(make_context(loop=asyncio.get_running_loop()), w)
        return w

    writer = asyncio.run(_run())
    assert writer.headers.get("Allow") == "GET, PATCH, POST"


def test_hostless_fallback_and_host_params() -> None:
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


def test_add_registers_route() -> None:
    router = Router()
    router.add(Route.post("/rooms/{room_id}/send"), noop_handler)
    _, match = router.find_handler("", "/rooms/7/send", "POST")
    assert match.pattern == "/rooms/{room_id}/send"
    assert dict(match.params) == {"room_id": "7"}


def test_rejects_duplicate_route() -> None:
    router = Router()
    router.get("/hello", noop_handler)
    with pytest.raises(StarioError, match="Route already registered"):
        router.get("/hello", noop_handler)


def test_cache_hit_returns_same_route_match() -> None:
    router = Router()
    router.get("/user/{user_id}", noop_handler)
    _, first = router.find_handler("", "/user/42", "GET")
    _, second = router.find_handler("", "/user/42", "GET")
    assert first is second
    assert dict(first.params) == {"user_id": "42"}


def test_use_applies_middleware() -> None:
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

    async def _run() -> None:
        await handler(make_context(loop=asyncio.get_running_loop()), DummyWriter())

    asyncio.run(_run())
    assert calls == ["scope"]


def test_query_registers_rfc_query_method() -> None:
    router = Router()
    router.query("/feed", noop_handler)
    _, feed = router.find_handler("", "/feed", "QUERY")
    assert feed.pattern == "/feed"


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


def test_app_trailing_slash_redirect_preserves_query() -> None:
    _ctx, writer = _run_app(
        lambda _app: None, "/search/", query={"q": "cats", "page": 2}
    )
    assert writer.status == 308
    assert writer.headers.unsafe_get(b"location") == b"/search?q=cats&page=2"


def test_app_params_and_http_exception() -> None:
    seen: list[str] = []

    async def handler(c, w):
        seen.append(c.route.params["user_id"])
        raise HttpException(422, "nope")

    _ctx, writer = _run_app(lambda app: app.get("/user/{user_id}", handler), "/user/42")
    assert seen == ["42"]
    assert writer.status == 422
    assert writer.body == "nope"


def test_app_redirect_and_client_disconnected() -> None:
    async def redirect(_c, _w):
        raise RedirectException(303, "/next")

    _ctx, writer = _run_app(lambda app: app.get("/go", redirect), "/go")
    assert writer.status == 303
    assert writer.headers.get("location") == "/next"

    async def gone(_c, _w):
        raise ClientDisconnected()

    _ctx, writer = _run_app(lambda app: app.get("/x", gone), "/x")
    assert writer.status is None
    assert writer.completed


def test_app_create_task_eager_start() -> None:
    async def _run() -> None:
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


def test_app_handler_must_send_response() -> None:
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


def test_app_on_error_mro_and_handler_failure() -> None:
    async def custom(_c, w, _exc):
        w.respond(b"handled", b"text/plain", 418)

    class MyValueError(ValueError):
        pass

    async def boom(_c, _w):
        raise MyValueError("subtype")

    _ctx, writer = _run_app(
        lambda app: (app.on_error(ValueError, custom), app.get("/x", boom)),
        "/x",
    )
    assert writer.status == 418
    assert writer.body == "handled"

    async def bad(_c, _w, _exc):
        raise RuntimeError("handler failed")

    async def orig(_c, _w):
        raise ValueError("original")

    _ctx, writer = _run_app(
        lambda app: (app.on_error(ValueError, bad), app.get("/x", orig)),
        "/x",
    )
    assert writer.status == 500
    assert writer.body == "Internal Server Error"


def test_app_requires_running_loop() -> None:
    with pytest.raises(StarioError, match="requires a running event loop"):
        App()


def test_python_405_identity_not_required_on_cython() -> None:
    """Cython 405 handlers are equivalent, not the Python lru_cache object."""
    py = PyRouter()
    cy = Router()
    py.get("/r", noop_handler)
    cy.get("/r", noop_handler)
    py_handler, _ = py.find_handler("", "/r", "POST")
    cy_handler, _ = cy.find_handler("", "/r", "POST")
    assert py_handler is py_method_not_allowed_handler(frozenset({"GET"}))
    assert cy_handler is method_not_allowed_handler(frozenset({"GET"}))
    assert py_handler is not cy_handler


@pytest.mark.asyncio
async def test_cython_app_plaintext_and_params_over_protocol() -> None:
    app = App()

    async def plaintext(_c, w):
        responses.text(w, "Hello, World!")

    async def get_user(c, w):
        responses.text(w, c.route.params["user_id"])

    app.get("/plaintext", plaintext)
    app.get("/user/{user_id}", get_user)

    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /plaintext HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        first = await read_response(reader)
        assert b"Hello, World!" in first

        writer.write(b"GET /user/42 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        second = await read_response(reader)
        assert b"42" in second
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_sync_handler_skips_create_task_over_protocol() -> None:
    app = App()
    tasks_before = []

    def plaintext(_c, w):
        tasks_before.append(len(app.tasks))
        responses.text(w, "sync-ok")

    app.get("/plaintext", plaintext)

    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /plaintext HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        body = await read_response(reader)
        assert b"sync-ok" in body
        writer.close()
        await writer.wait_closed()
    assert tasks_before == [0]
