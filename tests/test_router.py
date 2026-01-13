"""Tests for stario.http.router - Route registration and dispatch."""

import pytest

from stario.exceptions import StarioError
from stario.http.router import Router, _normalize_path
from stario.http.types import Context, Handler
from stario.http.writer import Writer


class TestNormalizePath:
    """Test path normalization."""

    def test_empty_path(self):
        assert _normalize_path("") == "/"

    def test_single_segment(self):
        assert _normalize_path("users") == "/users"

    def test_leading_slash(self):
        assert _normalize_path("/users") == "/users"

    def test_trailing_slash(self):
        assert _normalize_path("users/") == "/users"

    def test_both_slashes(self):
        assert _normalize_path("/users/") == "/users"

    def test_root_path(self):
        assert _normalize_path("/") == "/"

    def test_multiple_segments(self):
        assert _normalize_path("api/v1/users") == "/api/v1/users"


class TestRouterBasic:
    """Test basic Router functionality."""

    def test_create_router(self):
        router = Router()
        assert router.empty

    def test_register_get_route(self):
        router = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        router.get("/hello", handler)
        assert not router.empty
        assert "/hello" in router._exact

    def test_register_post_route(self):
        router = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        router.post("/submit", handler)
        assert "POST" in router._exact["/submit"]

    def test_register_all_methods(self):
        router = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        router.get("/a", handler)
        router.post("/b", handler)
        router.put("/c", handler)
        router.delete("/d", handler)
        router.patch("/e", handler)
        router.head("/f", handler)
        router.options("/g", handler)

        assert "GET" in router._exact["/a"]
        assert "POST" in router._exact["/b"]
        assert "PUT" in router._exact["/c"]
        assert "DELETE" in router._exact["/d"]
        assert "PATCH" in router._exact["/e"]
        assert "HEAD" in router._exact["/f"]
        assert "OPTIONS" in router._exact["/g"]


class TestRouterConflicts:
    """Test route conflict detection."""

    def test_duplicate_route_raises(self):
        router = Router()

        async def h1(c: Context, w: Writer) -> None:
            pass

        async def h2(c: Context, w: Writer) -> None:
            pass

        router.get("/hello", h1)

        with pytest.raises(StarioError, match="Route already registered"):
            router.get("/hello", h2)

    def test_same_path_different_method_ok(self):
        router = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        router.get("/resource", handler)
        router.post("/resource", handler)  # Should not raise

        assert "GET" in router._exact["/resource"]
        assert "POST" in router._exact["/resource"]


class TestRouterCatchAll:
    """Test catch-all route registration."""

    def test_catchall_registration(self):
        router = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        router.get("/files/*", handler)
        assert len(router._catchall) == 1
        assert router._catchall[0][0] == "/files"

    def test_catchall_sorted_by_prefix_length(self):
        router = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        router.get("/a/*", handler)
        router.get("/a/b/c/*", handler)
        router.get("/a/b/*", handler)

        # Longest prefix should be first
        assert router._catchall[0][0] == "/a/b/c"
        assert router._catchall[1][0] == "/a/b"
        assert router._catchall[2][0] == "/a"


class TestRouterMiddleware:
    """Test middleware handling."""

    def test_use_before_routes(self):
        router = Router()
        calls: list[str] = []

        def mw(handler: Handler) -> Handler:
            async def wrapper(c: Context, w: Writer) -> None:
                calls.append("mw")
                await handler(c, w)

            return wrapper

        router.use(mw)

        async def handler(c: Context, w: Writer) -> None:
            calls.append("handler")

        router.get("/test", handler)
        # Middleware should wrap handler

    def test_use_after_routes_raises(self):
        router = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        router.get("/test", handler)

        def mw(handler: Handler) -> Handler:
            return handler

        with pytest.raises(StarioError, match="Middleware must be registered before routes"):
            router.use(mw)

    def test_per_route_middleware(self):
        router = Router()
        calls: list[str] = []

        def route_mw(handler: Handler) -> Handler:
            async def wrapper(c: Context, w: Writer) -> None:
                calls.append("route_mw")
                await handler(c, w)

            return wrapper

        async def handler(c: Context, w: Writer) -> None:
            calls.append("handler")

        router.get("/test", handler, route_mw)
        # Per-route middleware should be applied


class TestRouterMount:
    """Test sub-router mounting."""

    def test_mount_adds_prefix(self):
        main = Router()
        api = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        api.get("/users", handler)
        main.mount("/api", api)

        assert "/api/users" in main._exact

    def test_mount_root_route(self):
        main = Router()
        api = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        api.get("/", handler)
        main.mount("/api", api)

        assert "/api" in main._exact

    def test_mount_catchall(self):
        main = Router()
        api = Router()

        async def handler(c: Context, w: Writer) -> None:
            pass

        api.get("/files/*", handler)
        main.mount("/api", api)

        # Should create /api/files catchall
        prefixes = [p for p, _ in main._catchall]
        assert "/api/files" in prefixes

    def test_mount_applies_parent_middleware(self):
        main = Router()
        calls: list[str] = []

        def parent_mw(handler: Handler) -> Handler:
            async def wrapper(c: Context, w: Writer) -> None:
                calls.append("parent")
                await handler(c, w)

            return wrapper

        main.use(parent_mw)

        api = Router()

        async def handler(c: Context, w: Writer) -> None:
            calls.append("handler")

        api.get("/test", handler)
        main.mount("/api", api)
        # Parent middleware should wrap mounted routes
