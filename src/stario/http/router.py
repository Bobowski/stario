"""
Router - Simple, explicit route registration.

No decorators, just functions. Closure-friendly.

Usage:
    router = Router()
    router.use(logging_mw)  # must be before routes
    router.get("/", home)
    router.get("/users/*", get_user)
    router.get("/admin", admin_handler, auth_mw)  # per-route middleware
    router.mount("/api", api_router)
    router.assets("/static", "./static")  # serve static files
"""

from pathlib import Path

from stario.exceptions import StarioError

from .staticassets import StaticAssets
from .types import Context, Handler, Middleware
from .writer import Writer


def _normalize_path(path: str) -> str:
    """Normalize path to canonical form."""
    if not path:
        return "/"
    path = "/" + path.strip("/")
    return path if path != "/" else "/"


class Router:
    """
    HTTP Router - explicit route registration.

    Supports:
    - Exact paths: /users, /api/v1
    - Catch-all: /static/* (r.tail = rest of path)
    - Sub-routers: mount("/api", api_router)
    - Per-route middleware: get("/admin", handler, auth_mw, logging_mw)

    Middleware rules:
    - Router-level middleware (use()) must be added before any routes
    - Per-route middleware is passed as extra args to route methods
    - Parent middleware applies to mounted sub-routers (auth cascades down)
    - Execution order: parent mw -> router mw -> route mw -> handler

    Usage:
        app = Router()
        app.use(auth_mw)  # applies to all routes including mounted

        api = Router()
        api.use(logging_mw)  # api-specific middleware
        api.get("/users", list_users)
        api.get("/admin", admin_panel, admin_only_mw)

        app.mount("/api", api)  # auth_mw wraps all api routes
    """

    __slots__ = (
        "_middlewares",
        "_exact",
        "_catchall",
    )

    def __init__(self) -> None:
        self._middlewares: list[Middleware] = []
        self._exact: dict[str, dict[str, Handler]] = {}
        self._catchall: list[tuple[str, dict[str, Handler]]] = []

    @property
    def empty(self) -> bool:
        """True if no routes have been registered."""
        return not self._exact and not self._catchall

    # =========================================================================
    # Configuration
    # =========================================================================

    def use(self, *middleware: Middleware) -> None:
        """
        Add middleware to this router.

        Must be called before any routes are registered.
        Middleware is applied in order (first added = outermost).
        """
        if not self.empty:
            raise StarioError(
                "Middleware must be registered before routes",
                context={"routes_registered": len(self._exact) + len(self._catchall)},
                help_text="Call router.use() before any router.get(), router.post(), etc.",
                example="""router = Router()
router.use(auth_middleware)  # First: middleware
router.get("/", home)        # Then: routes""",
            )
        self._middlewares.extend(middleware)

    def _wrap_handler(self, handler: Handler, *middleware: Middleware) -> Handler:
        """Apply router-level + per-route middleware to handler."""
        # Order: router mw (outer) -> route mw (inner) -> handler
        all_mw = list(self._middlewares) + list(middleware)
        for mw in reversed(all_mw):
            handler = mw(handler)
        return handler

    # =========================================================================
    # Route registration
    # =========================================================================

    def handle(
        self, method: str, path: str, handler: Handler, *middleware: Middleware
    ) -> None:
        """Register a handler for method + path with optional middleware."""
        path = _normalize_path(path)
        wrapped = self._wrap_handler(handler, *middleware)

        if path.endswith("/*"):
            prefix = path[:-2] or "/"
            self._add_catchall(prefix, method, wrapped)
        else:
            self._add_exact(path, method, wrapped)

    def _add_exact(self, path: str, method: str, handler: Handler) -> None:
        """Add exact path route."""
        methods = self._exact.setdefault(path, {})
        if method in methods:
            raise StarioError(
                f"Route already registered: {method} {path}",
                context={"method": method, "path": path},
                help_text="Each method + path combination can only have one handler.",
            )
        methods[method] = handler

    def _add_catchall(self, prefix: str, method: str, handler: Handler) -> None:
        """Add catch-all route."""
        for p, methods in self._catchall:
            if p == prefix:
                if method in methods:
                    raise StarioError(
                        f"Route already registered: {method} {prefix}/*",
                        context={"method": method, "prefix": prefix},
                        help_text="Each method + path combination can only have one handler.",
                    )
                methods[method] = handler
                return

        self._catchall.append((prefix, {method: handler}))
        # Sort by prefix length (longest first)
        self._catchall.sort(key=lambda x: len(x[0]), reverse=True)

    def get(self, path: str, handler: Handler, *middleware: Middleware) -> None:
        """Register GET handler."""
        self.handle("GET", path, handler, *middleware)

    def post(self, path: str, handler: Handler, *middleware: Middleware) -> None:
        """Register POST handler."""
        self.handle("POST", path, handler, *middleware)

    def put(self, path: str, handler: Handler, *middleware: Middleware) -> None:
        """Register PUT handler."""
        self.handle("PUT", path, handler, *middleware)

    def delete(self, path: str, handler: Handler, *middleware: Middleware) -> None:
        """Register DELETE handler."""
        self.handle("DELETE", path, handler, *middleware)

    def patch(self, path: str, handler: Handler, *middleware: Middleware) -> None:
        """Register PATCH handler."""
        self.handle("PATCH", path, handler, *middleware)

    def head(self, path: str, handler: Handler, *middleware: Middleware) -> None:
        """Register HEAD handler."""
        self.handle("HEAD", path, handler, *middleware)

    def options(self, path: str, handler: Handler, *middleware: Middleware) -> None:
        """Register OPTIONS handler."""
        self.handle("OPTIONS", path, handler, *middleware)

    # =========================================================================
    # Sub-routers
    # =========================================================================

    def mount(self, prefix: str, router: "Router") -> None:
        """
        Mount a sub-router at prefix.

        All routes in the sub-router are prefixed.
        Parent router middleware IS applied to all mounted routes.
        """
        prefix = _normalize_path(prefix)

        # Copy exact routes with prefix, applying parent middleware
        for path, methods in router._exact.items():
            full_path = _normalize_path(prefix + path)
            for method, handler in methods.items():
                wrapped = self._wrap_handler(handler)
                self._add_exact(full_path, method, wrapped)

        # Copy catchall routes with prefix, applying parent middleware
        for sub_prefix, methods in router._catchall:
            full_prefix = _normalize_path(prefix + sub_prefix)
            for method, handler in methods.items():
                wrapped = self._wrap_handler(handler)
                self._add_catchall(full_prefix, method, wrapped)

    def assets(
        self,
        path: str,
        directory: str | Path,
        *middleware: Middleware,
        collection: str | None = None,
        cache_control: str = "public, max-age=31536000, immutable",
    ) -> "StaticAssets":
        """
        Mount static assets at path.

        Creates a StaticAssets handler and registers it for GET and HEAD requests.
        The collection is registered globally for use with asset().

        Args:
            path: URL path prefix (e.g., "/static")
            directory: Local directory containing static files
            *middleware: Optional middleware to apply to asset requests
            collection: Collection name for asset() lookup (defaults to path)
            cache_control: Cache-Control header value

        Returns:
            StaticAssets instance

        Example:
            app.assets("/static", "./static")
            app.assets("/uploads", "./uploads", auth_mw, collection="uploads")

            # Then use anywhere:
            from stario.http.staticassets import asset
            asset("style.css")  # from "static" collection
        """
        if collection is None:
            collection = path.strip("/").split("/")[0] or "static"

        static = StaticAssets(directory, collection, cache_control)
        self.get(f"{path}/*", static, *middleware)
        self.head(f"{path}/*", static, *middleware)
        return static

    # =========================================================================
    # Dispatch
    # =========================================================================

    async def dispatch(self, c: Context, w: Writer) -> None:
        """Dispatch request to matching handler."""
        path = c.req.path
        method = c.req.method

        # Strip trailing slash (redirect)
        if path != "/" and path.endswith("/"):
            w.redirect(path.rstrip("/"), 301)
            return

        # Try exact match
        if methods := self._exact.get(path):
            if handler := methods.get(method):
                await handler(c, w)
                return
            # Method not allowed
            w.headers.set(b"allow", ", ".join(methods.keys()))
            w.text("Method Not Allowed", 405)
            return

        # Try prefix match (catch-all)
        for prefix, methods in self._catchall:
            if path.startswith(prefix):
                if handler := methods.get(method):
                    # Set param to rest of path
                    c.req.tail = path[len(prefix) :].lstrip("/")
                    await handler(c, w)
                    return
                # Method not allowed
                w.headers.set(b"allow", ", ".join(methods.keys()))
                w.text("Method Not Allowed", 405)
                return

        # Not found
        w.text("Not Found", 404)
