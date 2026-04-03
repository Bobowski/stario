"""
Router - Simple, explicit route registration.

No decorators, just functions. Closure-friendly.

Usage:
    router = Router()
    router.use(logging_mw)  # must be before routes
    router.get("/", home, name="home")
    router.get("/users/*", get_user, name="user_files")
    router.get("/admin", admin_handler, auth_mw, name="admin")
    router.mount("/api", api_router)
    router.assets("/static", "./static", name="static")
"""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from stario.exceptions import StarioError

from .staticassets import StaticAssets
from .types import Context, Handler, Middleware, UrlQueryParams
from .writer import Writer


def _normalize_path(path: str) -> str:
    """Normalize path to canonical form."""
    if not path:
        return "/"
    path = "/" + path.strip("/")
    return path if path != "/" else "/"


def _join_subpath(base: str, path: str | None) -> str:
    """Join an optional subpath onto a normalized base path."""
    if not path:
        return base

    suffix = path.strip("/")
    if not suffix:
        return base

    if base == "/":
        return "/" + suffix
    return f"{base.rstrip('/')}/{suffix}"


def _append_queries(url: str, queries: UrlQueryParams | None) -> str:
    """Append encoded query parameters to a URL."""
    if not queries:
        return url
    return f"{url}?{urlencode(queries, doseq=True)}"


_PARAM_PATTERN = re.compile(r"\{(\w+)\}")


def _has_param_syntax(path: str) -> bool:
    """Check if path contains {param} style parameters."""
    return "{" in path and "}" in path


def _convert_param_path(path: str) -> tuple[str, list[str], re.Pattern[str]]:
    """
    Convert {param} style path to regex pattern.

    Returns:
        tuple of (prefix_without_params, list of param names, compiled regex pattern)
    """
    param_names: list[str] = []
    regex_parts: list[str] = []
    last_end = 0

    for match in _PARAM_PATTERN.finditer(path):
        param_names.append(match.group(1))
        before = path[last_end : match.start()]
        regex_parts.append(re.escape(before) + r"(?P<" + match.group(1) + r">[^/]+)")
        last_end = match.end()

    if last_end < len(path):
        regex_parts.append(re.escape(path[last_end:]))

    regex_str = "".join(regex_parts) + "$"
    pattern = re.compile(regex_str)

    prefix = path[: path.find("{")].rstrip("/") if "{" in path else path
    if not prefix:
        prefix = "/"

    return prefix, param_names, pattern


@dataclass(slots=True, frozen=True)
class _ExactRoute:
    """Reverse route for an exact path."""

    prefix: str

    def url(self, path: str | None) -> str:
        if path not in (None, ""):
            raise StarioError(
                "Exact routes do not accept a path argument",
                context={"path": path},
                help_text="Pass the second argument only for wildcard routes or named asset mounts.",
            )
        return ""


@dataclass(slots=True, frozen=True)
class _WildcardRoute:
    """Reverse route for a catch-all path."""

    prefix: str

    def url(self, path: str | None) -> str:
        if not path:
            return ""
        return path.strip("/")


@dataclass(slots=True, frozen=True)
class _AssetRoute:
    """Reverse route for a named asset mount."""

    prefix: str
    static: StaticAssets

    def url(self, path: str | None) -> str:
        if not path:
            raise StarioError(
                "Named asset mounts require a path argument",
                help_text="Pass the logical asset path as the second argument, for example url_for('static', 'css/style.css').",
            )
        return self.static.url(path)


@dataclass(slots=True, frozen=True)
class _ParamRoute:
    """Reverse route for a parameterized path."""

    prefix: str
    param_names: list[str]
    pattern: re.Pattern[str]
    path_template: str = ""

    def url(self, path: dict[str, str] | None) -> str:
        if path is None:
            raise StarioError(
                "Parameterized routes require a path argument",
                help_text="Pass a dict with param values, for example url_for('game', {'gameId': '123'}).",
            )
        for name in self.param_names:
            if name not in path:
                raise StarioError(
                    f"Missing parameter: '{name}'",
                    context={
                        "route_params": self.param_names,
                        "provided": list(path.keys()),
                    },
                    help_text=f"All route parameters must be provided: {self.param_names}",
                )
        if self.path_template:
            result = self.path_template
            for name in self.param_names:
                result = result.replace("{" + name + "}", path[name])
            return result[len(self.prefix) :]  # Return only the part after prefix
        url = ""
        for name in self.param_names:
            url = f"{url}/{path[name]}"
        return url


type _ReverseRoute = _ExactRoute | _WildcardRoute | _AssetRoute | _ParamRoute


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
        "_param_routes",
        "_reverse_routes",
    )

    def __init__(self) -> None:
        self._middlewares: list[Middleware] = []
        self._exact: dict[str, dict[str, Handler]] = {}
        self._catchall: list[tuple[str, dict[str, Handler]]] = []
        self._param_routes: list[
            tuple[str, list[str], re.Pattern[str], dict[str, Handler]]
        ] = []
        self._reverse_routes: dict[str, _ReverseRoute] = {}

    @property
    def empty(self) -> bool:
        """True if no routes have been registered."""
        return not self._exact and not self._catchall and not self._param_routes

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
        self,
        method: str,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register a handler for method + path with optional middleware."""
        path = _normalize_path(path)
        wrapped = self._wrap_handler(handler, *middleware)

        if path.endswith("/*"):
            prefix = path[:-2] or "/"
            if name is not None:
                self._register_reverse_route(name, _WildcardRoute(prefix))
            self._add_catchall(prefix, method, wrapped)
        elif _has_param_syntax(path):
            prefix, param_names, pattern = _convert_param_path(path)
            if name is not None:
                self._register_reverse_route(
                    name, _ParamRoute(prefix, param_names, pattern, path)
                )
            self._add_param_route(prefix, method, wrapped, param_names, pattern)
        else:
            if name is not None:
                self._register_reverse_route(name, _ExactRoute(path))
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

    def _add_param_route(
        self,
        prefix: str,
        method: str,
        handler: Handler,
        param_names: list[str],
        pattern: re.Pattern[str],
    ) -> None:
        """Add parameterized route."""
        for p, _, existing_pattern, methods in self._param_routes:
            if p == prefix and existing_pattern.pattern == pattern.pattern:
                if method in methods:
                    raise StarioError(
                        f"Route already registered: {method} {pattern.pattern}",
                        context={"method": method, "pattern": pattern.pattern},
                        help_text="Each method + path combination can only have one handler.",
                    )
                methods[method] = handler
                return

        self._param_routes.append((prefix, param_names, pattern, {method: handler}))
        # Sort by prefix length (longest first)
        self._param_routes.sort(key=lambda x: len(x[0]), reverse=True)

    def _register_reverse_route(self, name: str, route: _ReverseRoute) -> None:
        """Register a reverse route in the flat name registry."""
        if name in self._reverse_routes:
            raise StarioError(
                f"Reverse route name already exists: '{name}'",
                help_text="Names must be unique.",
            )
        self._reverse_routes[name] = route

    def get(
        self,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register GET handler."""
        self.handle("GET", path, handler, *middleware, name=name)

    def post(
        self,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register POST handler."""
        self.handle("POST", path, handler, *middleware, name=name)

    def put(
        self,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register PUT handler."""
        self.handle("PUT", path, handler, *middleware, name=name)

    def delete(
        self,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register DELETE handler."""
        self.handle("DELETE", path, handler, *middleware, name=name)

    def patch(
        self,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register PATCH handler."""
        self.handle("PATCH", path, handler, *middleware, name=name)

    def head(
        self,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register HEAD handler."""
        self.handle("HEAD", path, handler, *middleware, name=name)

    def options(
        self,
        path: str,
        handler: Handler,
        *middleware: Middleware,
        name: str | None = None,
    ) -> None:
        """Register OPTIONS handler."""
        self.handle("OPTIONS", path, handler, *middleware, name=name)

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

        # Copy param routes with prefix, applying parent middleware
        for sub_prefix, param_names, pattern, methods in router._param_routes:
            full_prefix = _normalize_path(prefix + sub_prefix)
            for method, handler in methods.items():
                wrapped = self._wrap_handler(handler)
                self._add_param_route(
                    full_prefix, method, wrapped, param_names, pattern
                )

        for name, route in router._reverse_routes.items():
            full_path = _normalize_path(prefix + route.prefix)
            if isinstance(route, _ExactRoute):
                mounted = _ExactRoute(full_path)
            elif isinstance(route, _WildcardRoute):
                mounted = _WildcardRoute(full_path)
            elif isinstance(route, _ParamRoute):
                mounted = _ParamRoute(
                    full_path, route.param_names, route.pattern, route.path_template
                )
            else:
                mounted = _AssetRoute(full_path, route.static)
            self._register_reverse_route(name, mounted)

    def assets(
        self,
        path: str,
        directory: str | Path,
        *middleware: Middleware,
        name: str | None = None,
        cache_control: str = "public, max-age=31536000, immutable",
    ) -> "StaticAssets":
        """
        Mount static assets at path.

        Creates a StaticAssets handler and registers it for GET and HEAD requests.
        If name= is provided, the asset mount is added to the reverse-routing table.

        Args:
            path: URL path prefix (e.g., "/static")
            directory: Local directory containing static files
            *middleware: Optional middleware to apply to asset requests
            name: Optional flat reverse-routing name for url_for()
            cache_control: Cache-Control header value

        Returns:
            StaticAssets instance

        Example:
            app.assets("/static", "./static", name="static")
            app.url_for("static", "style.css")
        """
        static = StaticAssets(directory, cache_control=cache_control)
        normalized_path = _normalize_path(path)
        self.get(f"{path}/*", static, *middleware)
        self.head(f"{path}/*", static, *middleware)
        if name is not None:
            self._register_reverse_route(
                name,
                _AssetRoute(normalized_path, static),
            )
        return static

    def url_for(
        self,
        name: str,
        path: str | dict[str, str] | None = None,
        queries: UrlQueryParams | None = None,
    ) -> str:
        """
        Resolve a named route or asset to a public URL.

        Exact routes ignore `path`.
        Wildcard routes append `path` to the registered prefix.
        Asset mounts fingerprint and resolve the logical asset `path`.
        Parameterized routes require a dict of param values.
        """
        route = self._reverse_routes.get(name)
        if route is None:
            raise StarioError(
                f"Reverse route not registered: '{name}'",
                context={
                    "name": name,
                    "available": list(self._reverse_routes.keys())[:10],
                },
                help_text=f"Register the route or asset first with name='{name}' before calling url_for().",
                example="""app.get("/", home, name="home")
app.assets("/static", "./static", name="static")""",
            )

        if isinstance(route, _ParamRoute):
            if isinstance(path, dict):
                resolved_path = route.url(path)
            elif path is None:
                resolved_path = route.url(None)
            else:
                raise StarioError(
                    "Parameterized routes require a dict of param values",
                    help_text="Pass a dict, for example url_for('game', {'gameId': '123'}).",
                )
        else:
            if isinstance(path, dict):
                raise StarioError(
                    "Non-parameterized routes do not accept dict path",
                    help_text="Pass a string path or use parameterized routes.",
                )
            resolved_path = route.url(path)
        return _append_queries(_join_subpath(route.prefix, resolved_path), queries)

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
                    c.req.tail = path[len(prefix) :].lstrip("/")
                    await handler(c, w)
                    return
                w.headers.set(b"allow", ", ".join(methods.keys()))
                w.text("Method Not Allowed", 405)
                return

        # Try parameterized routes
        for prefix, _, pattern, methods in self._param_routes:
            if path.startswith(prefix):
                if match := pattern.match(path):
                    if handler := methods.get(method):
                        c.req.params = match.groupdict()
                        await handler(c, w)
                        return
                    w.headers.set(b"allow", ", ".join(methods.keys()))
                    w.text("Method Not Allowed", 405)
                    return

        # Not found
        w.text("Not Found", 404)
