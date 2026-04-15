"""Tests for the current trie-based HTTP router."""

import asyncio
from collections.abc import Awaitable, Coroutine
from io import StringIO
from typing import Any, cast

import pytest

from stario import App
from stario.exceptions import StarioError
from stario.http.context import Context
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request
from stario.http.router import (
    EMPTY_ROUTE_MATCH,
    Handler,
    Router,
    _normalize_path,
    default_not_found,
    find_handler,
    method_not_allowed_handler,
)
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


def make_context(
    path: str,
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
) -> Context:
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

    tracer = JsonTracer(StringIO())
    owned_app = app if app is not None else App()
    return Context(
        app=owned_app,
        req=Request(
            method=method,
            path=path,
            query_bytes=b"",
            headers=headers,
            body=reader,
        ),
        span=tracer.create("request"),
        state={},
    )


def _run(awaitable: Awaitable[None]) -> None:
    asyncio.run(cast(Coroutine[Any, Any, None], awaitable))


async def noop_handler(c: Context, w: Writer) -> None:
    return None


class TestNormalizePath:
    def test_normalize_path(self):
        assert _normalize_path("") == "/"
        assert _normalize_path("users") == "/users"
        assert _normalize_path("/users/") == "/users"
        assert _normalize_path("/") == "/"


class TestFindHandler:
    def test_matches_exact_route(self):
        router = Router()
        router.get("/hello", noop_handler)

        handler, match = find_handler("", "/hello", "GET", router.root)

        assert handler is not None
        assert match.pattern == "/hello"
        assert dict(match.params) == {}

    def test_matches_path_params(self):
        router = Router()
        router.get("/users/{user_id}/posts/{post_id}", noop_handler)

        _, match = find_handler("", "/users/42/posts/7", "GET", router.root)

        assert match.pattern == "/users/{user_id}/posts/{post_id}"
        assert dict(match.params) == {"user_id": "42", "post_id": "7"}

    def test_matches_catchall_params(self):
        router = Router()
        router.get("/files/{path...}", noop_handler)

        _, match = find_handler("", "/files/docs/readme.txt", "GET", router.root)

        assert match.pattern == "/files/{path...}"
        assert dict(match.params) == {"path": "docs/readme.txt"}

    def test_rejects_non_terminal_catchall(self):
        router = Router()

        with pytest.raises(
            StarioError, match="Catchall path param in invalid position"
        ):
            router.get("/files/{path...}/edit", noop_handler)

    def test_rejects_path_without_leading_slash(self):
        router = Router()

        with pytest.raises(StarioError, match=r"Expected '/users'"):
            router.get("users", noop_handler)

    def test_returns_not_found_handler_and_empty_match(self):
        router = Router()
        router.get("/hello", noop_handler)

        handler, match = find_handler("", "/missing", "GET", router.root)

        assert handler is default_not_found
        assert match is EMPTY_ROUTE_MATCH

    def test_returns_method_not_allowed_for_known_path(self):
        router = Router()
        router.get("/hello", noop_handler)

        handler, match = find_handler("", "/hello", "POST", router.root)

        assert handler is method_not_allowed_handler(frozenset({"GET"}))
        assert match is EMPTY_ROUTE_MATCH

    def test_method_not_allowed_allowed_methods_sorted_in_allow_header(self):
        router = Router()
        router.get("/r", noop_handler)
        router.post("/r", noop_handler)
        router.patch("/r", noop_handler)

        handler, _ = find_handler("", "/r", "DELETE", router.root)

        expected = method_not_allowed_handler(frozenset({"GET", "POST", "PATCH"}))
        assert handler is expected

        w = DummyWriter()
        _run(handler(make_context("/r", method="DELETE"), cast(Writer, w)))

        assert w.headers.get("Allow") == "GET, PATCH, POST"

    def test_method_not_allowed_leaves_route_match_empty(self):
        router = Router()
        router.get("/users/{user_id}", noop_handler)

        handler, match = find_handler("", "/users/42", "POST", router.root)

        assert handler is method_not_allowed_handler(frozenset({"GET"}))
        assert match is EMPTY_ROUTE_MATCH

    def test_literal_http_verb_segment_is_not_false_405(self):
        router = Router()
        router.get("/foo/POST", noop_handler)

        handler, match = find_handler("", "/foo", "GET", router.root)

        assert handler is default_not_found
        assert match is EMPTY_ROUTE_MATCH

    def test_matches_host_pattern(self):
        router = Router()
        router.get("api.example.com/users", noop_handler)

        _, match = find_handler("api.example.com", "/users", "GET", router.root)

        assert match.pattern == "api.example.com/users"
        assert dict(match.params) == {}

    def test_matches_host_pattern_case_insensitively(self):
        router = Router()
        router.get("API.Example.Com/users", noop_handler)

        _, match = find_handler("Api.Example.Com", "/users", "GET", router.root)

        assert match.pattern == "api.example.com/users"
        assert dict(match.params) == {}

    def test_path_only_routes_ignore_request_host(self):
        router = Router()
        router.get("/health", noop_handler)

        _, match = find_handler("api.example.com", "/health", "GET", router.root)

        assert match.pattern == "/health"
        assert dict(match.params) == {}

    def test_hostless_routes_fallback_when_no_host_branch_matches(self):
        router = Router()
        router.get("/health", noop_handler)
        router.get("api.example.com/users", noop_handler)

        _, match = find_handler("www.example.org", "/health", "GET", router.root)

        assert match.pattern == "/health"
        assert dict(match.params) == {}


class TestRouterMount:
    def test_mount_prefix_matches_child_routes(self):
        api = Router()
        api.get("/users/{user_id}", noop_handler)

        app = Router()
        app.mount("/api", api)

        _, match = find_handler("", "/api/users/42", "GET", app.root)

        assert match.pattern == "/api/users/{user_id}"
        assert dict(match.params) == {"user_id": "42"}

    def test_root_mount_merges_routes_directly(self):
        child = Router()
        child.get("/users", noop_handler)

        parent = Router()
        parent.mount("/", child)

        _, match = find_handler("", "/users", "GET", parent.root)

        assert match.pattern == "/users"

    def test_mount_prefix_matches_child_root_route_at_prefix(self):
        child = Router()
        child.get("/", noop_handler)

        parent = Router()
        parent.mount("/home", child)

        _, match = find_handler("", "/home", "GET", parent.root)

        assert match.pattern == "/home"

    def test_mount_applies_parent_middleware(self):
        calls: list[str] = []

        def parent_middleware(handler: Handler) -> Handler:
            async def wrapped(c: Context, w: Writer) -> None:
                calls.append("parent")
                await handler(c, w)

            return wrapped

        async def child_handler(c: Context, w: Writer) -> None:
            calls.append("child")

        app = Router(middleware=[parent_middleware])
        api = Router()
        api.get("/users", child_handler)
        app.mount("/api", api)

        handler, _ = find_handler("", "/api/users", "GET", app.root)
        writer = DummyWriter()
        _run(handler(make_context("/api/users"), cast(Writer, writer)))

        assert calls == ["parent", "child"]

    def test_mount_applies_mount_specific_middleware(self):
        calls: list[str] = []

        def mount_middleware(handler: Handler) -> Handler:
            async def wrapped(c: Context, w: Writer) -> None:
                calls.append("mount")
                await handler(c, w)

            return wrapped

        async def child_handler(c: Context, w: Writer) -> None:
            calls.append("child")

        app = Router()
        api = Router()
        api.get("/users", child_handler)
        app.mount("/api", api, middleware=[mount_middleware])

        handler, _ = find_handler("", "/api/users", "GET", app.root)
        writer = DummyWriter()
        _run(handler(make_context("/api/users"), cast(Writer, writer)))

        assert calls == ["mount", "child"]

    def test_constructor_middleware_then_push_middleware(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router(middleware=[track("ctor")])
        router.get("/", noop_handler)
        router.push_middleware(track("used"))
        handler, _ = find_handler("", "/", "GET", router.root)
        _run(handler(make_context("/"), cast(Writer, DummyWriter())))
        # push_middleware re-wraps outside existing composition, so it runs before ctor here.
        assert calls == ["used", "ctor"]

    def test_push_middleware_applies_to_existing_routes(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router()
        router.get("/", noop_handler)
        router.push_middleware(track("mw"))
        handler, _ = find_handler("", "/", "GET", router.root)
        _run(handler(make_context("/"), cast(Writer, DummyWriter())))
        assert calls == ["mw"]

    def test_push_middleware_applies_to_routes_registered_later(self):
        calls: list[str] = []

        def mw(handler: Handler) -> Handler:
            async def wrapped(c: Context, w: Writer) -> None:
                calls.append("mw")
                await handler(c, w)

            return wrapped

        router = Router()
        router.push_middleware(mw)
        router.get("/", noop_handler)
        handler, _ = find_handler("", "/", "GET", router.root)
        _run(handler(make_context("/"), cast(Writer, DummyWriter())))
        assert calls == ["mw"]

    def test_router_middleware_stack_lifo_inbound(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router(middleware=[track("mw1"), track("mw2")])
        router.get("/", noop_handler)
        handler, _ = find_handler("", "/", "GET", router.root)
        _run(handler(make_context("/"), cast(Writer, DummyWriter())))
        # Stack: push mw1, push mw2 — request from top: mw2, mw1.
        assert calls == ["mw2", "mw1"]

    def test_route_middleware_stack_after_router_pushes(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router(middleware=[track("router")])
        router.get(
            "/",
            noop_handler,
            middleware=[track("auth"), track("rate")],
        )
        handler, _ = find_handler("", "/", "GET", router.root)
        _run(handler(make_context("/"), cast(Writer, DummyWriter())))
        # Pushes: router, auth, rate — top rate; inbound rate, auth, router.
        assert calls == ["rate", "auth", "router"]

    def test_push_middleware_multiple_order(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router()
        router.get("/", noop_handler)
        router.push_middleware(track("outer"), track("inner"))
        handler, _ = find_handler("", "/", "GET", router.root)
        _run(handler(make_context("/"), cast(Writer, DummyWriter())))
        # Pushes outer then inner — inner on top.
        assert calls == ["inner", "outer"]

    def test_push_middleware_noop_returns_none(self):
        router = Router()
        assert router.push_middleware() is None

    def test_push_middleware_after_mount_wraps_mounted_routes(self):
        calls: list[str] = []

        def parent_middleware(handler: Handler) -> Handler:
            async def wrapped(c: Context, w: Writer) -> None:
                calls.append("parent")
                await handler(c, w)

            return wrapped

        async def child_handler(c: Context, w: Writer) -> None:
            calls.append("child")

        child = Router()
        child.get("/users", child_handler)
        app = Router()
        app.mount("/api", child)
        app.push_middleware(parent_middleware)

        handler, _ = find_handler("", "/api/users", "GET", app.root)
        _run(handler(make_context("/api/users"), cast(Writer, DummyWriter())))

        assert calls == ["parent", "child"]

    def test_mount_prefixes_host_routes_after_host_match(self):
        child = Router()
        child.get("api.example.com/users", noop_handler)

        parent = Router()
        parent.mount("/v1", child)

        _, match = find_handler("api.example.com", "/v1/users", "GET", parent.root)

        assert match.pattern == "api.example.com/v1/users"

    def test_mount_exact_host_matches_path_only_child_routes(self):
        child = Router()
        child.get("/users", noop_handler)

        parent = Router()
        parent.mount("example.com/", child)

        _, match = find_handler("example.com", "/users", "GET", parent.root)

        assert match.pattern == "example.com/users"

    def test_mount_exact_host_does_not_match_subdomains_for_path_only_child_routes(
        self,
    ):
        child = Router()
        child.get("/users", noop_handler)

        parent = Router()
        parent.mount("example.com/", child)

        handler, match = find_handler("api.example.com", "/users", "GET", parent.root)

        assert handler is default_not_found
        assert match is EMPTY_ROUTE_MATCH

    def test_mount_rejects_host_prefix_for_host_aware_child_routes(self):
        child = Router()
        child.get("api/users", noop_handler)

        parent = Router()

        with pytest.raises(StarioError, match="Host matching already defined"):
            parent.mount("example.com/v1", child)

    def test_mount_rejects_non_router(self):
        parent = Router()

        with pytest.raises(
            StarioError,
            match="Can only mount subtrees",
        ):
            parent.mount("/api", object())  # type: ignore[arg-type]

    def test_mount_rejects_host_without_trailing_slash(self):
        parent = Router()
        child = Router()

        with pytest.raises(StarioError, match=r"Expected 'api\.example\.com/'"):
            parent.mount("api.example.com", child)

    def test_mount_rejects_non_terminal_catchall_prefix(self):
        parent = Router()
        child = Router()

        with pytest.raises(
            StarioError, match="Catchall path param in invalid position"
        ):
            parent.mount("/api/{rest...}/users", child)

    def test_mount_rejects_duplicate_child_names(self):
        parent = Router()
        parent.get("/health", noop_handler, name="dup")

        child = Router()
        child.get("/users", noop_handler, name="dup")

        with pytest.raises(StarioError, match="Name already registered"):
            parent.mount("/api", child)

    def test_mount_surfaces_conflicting_wildcards(self):
        parent = Router()
        parent.get("/users/{user_id}", noop_handler)

        child = Router()
        child.get("/users/{id}", noop_handler)

        with pytest.raises(StarioError, match="Nodes have conflicting wildcards"):
            parent.mount("/", child)


class TestAppUrlFor:
    def test_url_for_exact_route(self):
        app = App()
        app.get("/", noop_handler, name="home")

        assert app.url_for("home") == "/"

    def test_url_for_route_params_and_query(self):
        app = App()
        app.get("/users/{user_id}", noop_handler, name="user")

        assert (
            app.url_for(
                "user",
                params={"user_id": "42"},
                query={"page": 2, "tags": ["a", "b"]},
            )
            == "/users/42?page=2&tags=a&tags=b"
        )

    def test_url_for_catchall_route(self):
        app = App()
        app.get("/files/{path...}", noop_handler, name="files")

        assert (
            app.url_for("files", params={"path": "docs/readme.txt"})
            == "/files/docs/readme.txt"
        )

    def test_url_for_host_route_includes_host_and_path(self):
        app = App()
        app.get("api.example.com/users", noop_handler, name="users")

        assert app.url_for("users") == "api.example.com/users"

    def test_url_for_mounted_router_includes_prefix(self):
        chat = Router()
        chat.get("/", noop_handler, name="home")

        app = App()
        app.mount("/chat", chat)

        assert app.url_for("home") == "/chat"

    def test_url_for_host_mounted_router_includes_host_and_path(self):
        chat = Router()
        chat.get("/users", noop_handler, name="users")

        app = App()
        app.mount("example.com/v1", chat)

        assert app.url_for("users") == "example.com/v1/users"

    def test_url_for_missing_name_raises(self):
        app = App()

        with pytest.raises(StarioError, match="Reverse route not registered"):
            app.url_for("missing")

    def test_duplicate_name_raises_before_mutating_tree(self):
        router = Router()
        router.get("/first", noop_handler, name="dup")

        with pytest.raises(StarioError, match="Name already registered"):
            router.get("/second", noop_handler, name="dup")

        handler, match = find_handler("", "/second", "GET", router.root)
        assert handler is default_not_found
        assert match is EMPTY_ROUTE_MATCH


class TestRouterDispatch:
    def test_context_starts_with_empty_route_match(self):
        context = make_context("/hello")

        assert context.route is EMPTY_ROUTE_MATCH

    def test_dispatch_sets_route_pattern_and_params(self):
        seen: list[tuple[str, dict[str, str]]] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append((c.route.pattern, dict(c.route.params)))

        app = App()
        app.get("/users/{user_id}", handler)
        context = make_context("/users/42", app=app)

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "/users/{user_id}"
        assert dict(context.route.params) == {"user_id": "42"}
        assert seen == [("/users/{user_id}", {"user_id": "42"})]

    def test_dispatch_sets_mounted_route_pattern(self):
        seen: list[str] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append(c.route.pattern)

        api = Router()
        api.get("/users/{user_id}", handler)

        app = App()
        app.mount("/api", api)
        context = make_context("/api/users/42", app=app)

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "/api/users/{user_id}"
        assert seen == ["/api/users/{user_id}"]

    def test_dispatch_matches_mounted_root_route_without_trailing_slash(self):
        seen: list[str] = []

        async def handler(c: Context, w: Writer) -> None:
            seen.append(c.route.pattern)

        child = Router()
        child.get("/", handler)

        app = App()
        app.mount("/home", child)
        context = make_context("/home", app=app)

        writer = DummyWriter()
        _run(app(context, cast(Writer, writer)))

        assert context.route.pattern == "/home"
        assert seen == ["/home"]

    def test_dispatch_uses_not_found_handler(self):
        app = App()
        writer = DummyWriter()

        _run(app(make_context("/missing", app=app), cast(Writer, writer)))

        assert writer.status == 404
        assert writer.body == "Not Found"

    def test_dispatch_redirects_trailing_slash(self):
        app = App()
        writer = DummyWriter()

        _run(app(make_context("/users/", app=app), cast(Writer, writer)))

        assert writer.status == 308
        assert writer.headers.get("location") == "/users"

    def test_dispatch_preserves_method_for_trailing_slash_redirects(self):
        app = App()
        writer = DummyWriter()

        _run(app(make_context("/users/", method="POST", app=app), cast(Writer, writer)))

        assert writer.status == 308
        assert writer.headers.get("location") == "/users"

    def test_dispatch_trailing_slash_redirect_normalizes_leading_slashes(self):
        """``//host/`` must not become a protocol-relative Location (``//host``)."""
        app = App()
        writer = DummyWriter()

        _run(app(make_context("//aftra.io/", app=app), cast(Writer, writer)))

        assert writer.status == 308
        assert writer.headers.get("location") == "/aftra.io"

    def test_dispatch_uses_method_not_allowed_handler(self):
        app = App()
        app.get("/hello", noop_handler)
        writer = DummyWriter()
        context = make_context("/hello", method="POST", app=app)

        _run(app(context, cast(Writer, writer)))

        assert context.route is EMPTY_ROUTE_MATCH
        assert writer.status == 405
        assert writer.body == "Method Not Allowed"
        assert writer.headers.get("Allow") == "GET"
