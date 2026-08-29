"""Tests for the trie-based HTTP router."""

from functools import partial

import pytest

from stario.exceptions import StarioError
from stario.http import (
    Router,
    default_not_found,
    handler_is_async,
    method_not_allowed_handler,
)
from stario.http.context import EMPTY_ROUTE_MATCH, Context, Handler
from stario.http.writer import Writer
from stario.routing import Route, UrlPath
from tests.helpers import DummyWriter, run_handler, run_with_app


async def noop_handler(c: Context, w: Writer) -> None:
    return None


def sync_noop(c: Context, w: Writer) -> None:
    return None


class TestFindHandler:
    def test_matches_path_params(self):
        router = Router()
        router.get("/users/{user_id}/posts/{post_id}", noop_handler)

        _, match = router.find_handler("", "/users/42/posts/7", "GET")

        assert match.pattern == "/users/{user_id}/posts/{post_id}"
        assert dict(match.params) == {"user_id": "42", "post_id": "7"}

    def test_matches_catchall_params(self):
        router = Router()
        router.get("/files/{path...}", noop_handler)

        _, match = router.find_handler("", "/files/docs/readme.txt", "GET")

        assert match.pattern == "/files/{path...}"
        assert dict(match.params) == {"path": "docs/readme.txt"}

    def test_rejects_non_terminal_catchall(self):
        router = Router()

        with pytest.raises(
            StarioError, match="Catchall path param in invalid position"
        ):
            router.get("/files/{path...}/edit", noop_handler)

    def test_method_not_allowed_allowed_methods_sorted_in_allow_header(self):
        router = Router()
        router.get("/r", noop_handler)
        router.post("/r", noop_handler)
        router.patch("/r", noop_handler)

        handler, _ = router.find_handler("", "/r", "DELETE")

        w = DummyWriter()
        run_handler(handler, "/r", method="DELETE", writer=w)
        assert w.headers.get("Allow") == "GET, PATCH, POST"

    def test_literal_http_verb_segment_is_not_false_405(self):
        router = Router()
        router.get("/foo/POST", noop_handler)

        handler, match = router.find_handler("", "/foo", "GET")

        assert handler is default_not_found
        assert match is EMPTY_ROUTE_MATCH

    def test_matches_host_pattern_case_insensitively(self):
        router = Router()
        router.get(UrlPath("/users", host="API.Example.Com"), noop_handler)

        _, match = router.find_handler("api.example.com", "/users", "GET")

        assert match.pattern == "api.example.com/users"

    def test_hostless_routes_fallback_when_no_host_branch_matches(self):
        router = Router()
        router.get("/health", noop_handler)
        router.get(UrlPath("/users", host="api.example.com"), noop_handler)

        _, match = router.find_handler("www.example.org", "/health", "GET")

        assert match.pattern == "/health"

    def test_rejects_duplicate_route_registration(self):
        router = Router()
        router.get("/hello", noop_handler)

        with pytest.raises(StarioError, match="Route already registered"):
            router.get("/hello", noop_handler)

    def test_add_registers_route_method_and_path(self):
        send = Route.post("/rooms/{room_id}/send")
        router = Router()
        router.add(send, noop_handler)

        _, match = router.find_handler("", "/rooms/7/send", "POST")

        assert match.pattern == "/rooms/{room_id}/send"
        assert dict(match.params) == {"room_id": "7"}

        handler, _ = router.find_handler("", "/rooms/7/send", "GET")
        assert handler is method_not_allowed_handler(frozenset({"POST"}))

    def test_query_registers_rfc_query_method(self):
        router = Router()
        router.query("/feed", noop_handler)
        router.add(Route.query("/search"), noop_handler)

        _, feed = router.find_handler("", "/feed", "QUERY")
        _, search = router.find_handler("", "/search", "QUERY")

        assert feed.pattern == "/feed"
        assert search.pattern == "/search"

    def test_add_rejects_non_route(self):
        router = Router()

        with pytest.raises(StarioError, match="takes a Route"):
            router.add("/health", noop_handler)  # type: ignore[arg-type]

    def test_hostless_method_wins_over_host_405(self):
        router = Router()
        router.get(UrlPath("/api", host="api.example.com"), noop_handler)
        router.post("/api", noop_handler)

        handler, match = router.find_handler("api.example.com", "/api", "POST")

        assert match.pattern == "/api"
        assert handler is not method_not_allowed_handler(frozenset({"GET"}))

    def test_host_405_when_only_host_tree_knows_path(self):
        router = Router()
        router.get(UrlPath("/api", host="api.example.com"), noop_handler)

        handler, match = router.find_handler("api.example.com", "/api", "POST")

        assert handler is method_not_allowed_handler(frozenset({"GET"}))
        assert match is EMPTY_ROUTE_MATCH


class TestRouterUse:
    def test_use_applies_middleware_to_later_routes(self):
        calls: list[str] = []

        def scope_middleware(handler: Handler) -> Handler:
            async def wrapped(c: Context, w: Writer) -> None:
                calls.append("scope")
                await handler(c, w)

            return wrapped

        router = Router()
        router.use("/", scope_middleware)
        router.get("/users", noop_handler)

        handler, _ = router.find_handler("", "/users", "GET")
        run_handler(handler, "/users")

        assert calls == ["scope"]

    def test_use_rejects_middleware_after_routes(self):
        router = Router()
        router.get("/users", noop_handler)

        with pytest.raises(
            StarioError, match="Middleware must be registered before matching routes"
        ):
            router.use("/users", lambda h: h)

    def test_use_middleware_runs_in_registration_order(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router()
        router.use("/", track("mw1"), track("mw2"))
        router.get("/", noop_handler)
        handler, _ = router.find_handler("", "/", "GET")
        run_handler(handler, "/")
        assert calls == ["mw1", "mw2"]

    def test_nested_use_scopes_run_general_to_specific(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router()
        router.use("/", track("root"))
        router.use("/users", track("users"))
        router.get("/users/panel", noop_handler)
        handler, _ = router.find_handler("", "/users/panel", "GET")
        run_handler(handler, "/users/panel")
        assert calls == ["root", "users"]

    def test_route_middleware_runs_after_scope_middleware(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router()
        router.use("/", track("scope"))
        router.get(
            "/",
            noop_handler,
            middleware=[track("route")],
        )
        handler, _ = router.find_handler("", "/", "GET")
        run_handler(handler, "/")
        assert calls == ["scope", "route"]

    def test_host_route_inherits_hostless_path_middleware(self):
        calls: list[str] = []

        def track(label: str):
            def deco(handler: Handler) -> Handler:
                async def wrapped(c: Context, w: Writer) -> None:
                    calls.append(label)
                    await handler(c, w)

                return wrapped

            return deco

        router = Router()
        router.use("/", track("root"))
        router.use("/users", track("users"))
        router.get(UrlPath("/users", host="api.example.com"), noop_handler)

        handler, _ = router.find_handler("api.example.com", "/users", "GET")
        run_handler(handler, "/users", host="api.example.com")
        assert calls == ["root", "users"]

    def test_custom_not_found_on_host_branch_wins_over_hostless_miss(self):
        calls: list[str] = []

        async def host_not_found(c: Context, w: Writer) -> None:
            calls.append("host-404")

        router = Router()
        router.not_found(UrlPath("/", host="api.example.com"), host_not_found)
        router.get("/health", noop_handler)

        handler, match = router.find_handler("api.example.com", "/missing", "GET")
        assert match is EMPTY_ROUTE_MATCH
        run_handler(handler, "/missing", host="api.example.com")
        assert calls == ["host-404"]


class TestHandlerClassification:
    def test_async_def_is_async(self):
        assert handler_is_async(noop_handler) is True

    def test_sync_def_is_sync(self):
        assert handler_is_async(sync_noop) is False

    def test_partial_of_async_is_async(self):
        async def takes_flag(c: Context, w: Writer, _flag: int) -> None:
            return None

        assert handler_is_async(partial(takes_flag, _flag=1)) is True

    def test_callable_object_async_call(self):
        class Home:
            async def __call__(self, c: Context, w: Writer) -> None:
                return None

        assert handler_is_async(Home()) is True

    def test_rejects_non_callable(self):
        with pytest.raises(StarioError, match="Handler must be callable"):
            handler_is_async("not-a-handler")  # type: ignore[arg-type]

    def test_rejects_async_generator(self):
        async def stream(c: Context, w: Writer):
            yield None

        with pytest.raises(StarioError, match="async generator"):
            handler_is_async(stream)

    def test_registration_classifies_sync_and_async(self):
        router = Router()
        router.get("/async", noop_handler)
        router.post("/sync", sync_noop)

        _handler, _match, async_flag = router.resolve("", "/async", "GET")
        assert async_flag is True
        _handler, _match, sync_flag = router.resolve("", "/sync", "POST")
        assert sync_flag is False

    def test_default_not_found_is_sync(self):
        router = Router()
        _handler, _match, is_async = router.resolve("", "/missing", "GET")
        assert _handler is default_not_found
        assert is_async is False

    def test_default_method_not_allowed_is_sync(self):
        router = Router()
        router.get("/r", noop_handler)
        handler, _match, is_async = router.resolve("", "/r", "POST")
        assert handler is method_not_allowed_handler(frozenset({"GET"}))
        assert is_async is False

    def test_sync_handler_with_middleware_becomes_async(self):
        def scope_middleware(handler: Handler) -> Handler:
            async def wrapped(c: Context, w: Writer) -> None:
                await handler(c, w)

            return wrapped

        router = Router()
        router.use("/", scope_middleware)
        router.get("/users", sync_noop)

        _handler, _match, is_async = router.resolve("", "/users", "GET")
        assert is_async is True
        run_handler(_handler, "/users")

    def test_get_rejects_non_callable(self):
        router = Router()
        with pytest.raises(StarioError, match="Handler must be callable"):
            router.get("/x", "nope")  # type: ignore[arg-type]


class TestRouterDispatch:
    def test_dispatch_trailing_slash_redirect_normalizes_leading_slashes(self):
        """`//host/` must not become a protocol-relative Location (`//host`)."""
        _context, writer = run_with_app(lambda _app: None, "//aftra.io/")

        assert writer.status == 308
        assert writer.headers.get("location") == "/aftra.io"
