"""Route table: register `Route` values on a trie and resolve `(host, path, method)`."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache, partial

from stario_cython.exchange import compile_router

from typing_extensions import deprecated

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.context import Context, Handler, Match, Middleware
from stario.http.route import EMPTY_ROUTE, Route, UrlPath, as_target
from stario.http.segment import Segment
from stario.http.writer import Writer

type MethodNotAllowedHandler = Callable[[frozenset[str]], Handler]
type RouteMatch = tuple[Handler, Route, Match]


def require_async_handler(handler: object, *, what: str = "Handler") -> None:
    """Reject anything that is not an async callable (checked at registration)."""
    if not callable(handler):
        raise StarioError(
            f"{what} must be callable",
            context={"got": type(handler).__name__},
            help_text="Register `async def handler(c, w):`.",
        )

    fn: object = handler
    while isinstance(fn, partial):
        fn = fn.func

    if inspect.isasyncgenfunction(fn):
        raise StarioError(
            f"{what} cannot be an async generator",
            help_text="Use `async def handler(c, w):` and write to the Writer, not `yield`.",
        )
    if inspect.iscoroutinefunction(fn):
        return

    call = getattr(fn, "__call__", None)
    if call is not None and call is not fn:
        if inspect.isasyncgenfunction(call):
            raise StarioError(
                f"{what} cannot be an async generator",
                help_text="Use `async def __call__(self, c, w):` and write to the Writer, not `yield`.",
            )
        if inspect.iscoroutinefunction(call):
            return

    raise StarioError(
        f"{what} must be async",
        context={"got": type(handler).__name__},
        help_text="Register `async def handler(c, w):`. Sync handlers are not supported.",
    )


def _exact_nodes() -> dict[str, Node]:
    return {}


async def default_not_found(_c: Context, w: Writer) -> None:
    responses.text(w, "Not Found", 404)


@lru_cache(maxsize=256)
def method_not_allowed_handler(allowed: frozenset[str]) -> Handler:
    allow_header = ", ".join(sorted(allowed))

    async def respond(_c: Context, w: Writer) -> None:
        w.headers.set("Allow", allow_header)
        responses.text(w, "Method Not Allowed", 405)

    return respond


@dataclass(slots=True)
class Endpoint:
    handler: Handler
    route: Route


@dataclass(slots=True)
class Node:
    not_found_handler: Handler | None = None
    method_not_allowed_handler: MethodNotAllowedHandler | None = None
    middleware: tuple[Middleware, ...] = ()
    exact: dict[str, Node] = field(default_factory=_exact_nodes)
    wildcard_name: str | None = None
    wildcard: Node | None = None
    catchall_name: str | None = None
    catchall: Node | None = None
    endpoints: dict[str, Endpoint] | None = None


def _param_child(current: Node, segment: Segment) -> Node | None:
    if segment.kind == "catchall":
        if current.catchall is None or current.catchall_name != segment.name:
            return None
        return current.catchall
    if current.wildcard is None or current.wildcard_name != segment.name:
        return None
    return current.wildcard


def _step(
    current: Node, segs: tuple[Segment, ...], i: int
) -> tuple[Node, int] | None:
    seg = segs[i]
    if seg.kind == "exact":
        child = current.exact.get(seg.name)
    else:
        child = _param_child(current, seg)
    if child is None:
        return None
    return child, i + 1


def _collect_middleware(
    tree: Node,
    host_segments: tuple[Segment, ...],
    path_segments: tuple[Segment, ...],
) -> list[Middleware]:
    middlewares: list[Middleware] = list(tree.middleware)
    current = tree

    def follow(segs: tuple[Segment, ...], *, halt: bool) -> bool:
        nonlocal current
        i = 0
        while i < len(segs):
            stepped = _step(current, segs, i)
            if stepped is None:
                return halt
            current, i = stepped
            middlewares.extend(current.middleware)
        return False

    if follow(host_segments[::-1], halt=True):
        return middlewares
    follow(path_segments, halt=False)
    return middlewares


def _branch_has_endpoints(node: Node) -> bool:
    if node.endpoints:
        return True
    return (
        (node.wildcard is not None and _branch_has_endpoints(node.wildcard))
        or (node.catchall is not None and _branch_has_endpoints(node.catchall))
        or any(_branch_has_endpoints(child) for child in node.exact.values())
    )


def _open_param(current: Node, segment: Segment) -> Node:
    child = _param_child(current, segment)
    if child is not None:
        return child
    catchall = segment.kind == "catchall"
    taken = current.catchall_name if catchall else current.wildcard_name
    other = current.wildcard if catchall else current.catchall
    if taken is not None:
        raise StarioError(
            "Catchall parameter conflict"
            if catchall
            else "Wildcard parameter conflict",
            context={
                "existing": taken,
                "new": segment.name,
                "segment": segment.pattern,
            },
        )
    if other is not None:
        raise StarioError(
            "Ambiguous route parameter branch",
            context={
                "existing": current.wildcard_name
                if catchall
                else current.catchall_name,
                "new": segment.name,
            },
        )
    child = Node()
    if catchall:
        current.catchall_name = segment.name
        current.catchall = child
    else:
        current.wildcard_name = segment.name
        current.wildcard = child
    return child


def _descend_or_create(current: Node, segment: Segment) -> Node:
    if segment.kind != "exact":
        return _open_param(current, segment)
    child = current.exact.get(segment.name)
    if child is None:
        child = Node()
        current.exact[segment.name] = child
    return child


class Router:
    """Route table: host routes override hostless defaults when they fully match."""

    __slots__ = (
        "_has_param_hosts",
        "_host_routing",
        "_hosts_exact",
        "_hosts_param",
        "_cy_router",
        "_lookup",
        "_path",
    )

    def __init__(self) -> None:
        self._path = Node()
        self._hosts_exact: dict[str, Node] = {}
        self._hosts_param = Node()
        self._has_param_hosts = False
        self._host_routing = False
        self._cy_router = compile_router(self)
        self._lookup = self._cy_router.lookup

    @property
    def host_routing(self) -> bool:
        return self._host_routing

    def find_handler(self, host: str, path: str, method: str) -> RouteMatch:
        """Resolve `(host, path, method)`.

        `host` must already be lowercased (`Request.host`). `path` is the
        request path as sent (`Request.raw_path`, still percent-encoded): it is
        split on `/` first and each segment is decoded on its own, so `%2F`
        never adds a segment. Plain paths like `/users/42` are the same either
        way.
        """
        return self._lookup(host, path, method)

    def _invalidate_lookup(self) -> None:
        self._cy_router = compile_router(self)
        self._lookup = self._cy_router.lookup
        state = getattr(self, "_app_state", None)
        if state is not None:
            state.host_routing = self._host_routing
            state.router = self._cy_router

    def _tree_for(self, route: Route) -> Node:
        if not route.host:
            return self._path
        self._host_routing = True
        if all(segment.kind == "exact" for segment in route.host_segments):
            return self._hosts_exact.setdefault(route.host, Node())
        self._has_param_hosts = True
        return self._hosts_param

    def _descend(self, tree: Node, route: Route) -> Node:
        current = tree
        if tree is self._hosts_param:
            for segment in route.host_segments[::-1]:
                current = _descend_or_create(current, segment)
        for segment in route.path_segments:
            current = _descend_or_create(current, segment)
        return current

    def _policy_node(self, pattern: UrlPath | str) -> Node:
        route = Route(as_target(pattern))
        if any(segment.kind == "catchall" for segment in route.host_segments):
            raise StarioError(
                "Catchall host policy is not supported",
                context={"target": route.target},
            )
        if route.path_segments and route.path_segments[-1].kind == "catchall":
            raise StarioError(
                "Catchall route policy cannot have child routes",
                context={"target": route.target},
            )
        tree = self._tree_for(route)
        return self._descend(tree, route)

    def use(self, pattern: UrlPath | str, *middleware: Middleware) -> None:
        current = self._policy_node(pattern)
        if not middleware:
            return
        if _branch_has_endpoints(current):
            raise StarioError(
                "Middleware must be registered before matching routes",
                context={"pattern": pattern},
            )
        current.middleware = current.middleware + tuple(middleware)
        self._invalidate_lookup()

    def not_found(self, pattern: UrlPath | str, handler: Handler) -> None:
        require_async_handler(handler, what="Not-found handler")
        self._policy_node(pattern).not_found_handler = handler
        self._invalidate_lookup()

    def method_not_allowed(
        self,
        pattern: UrlPath | str,
        handler: MethodNotAllowedHandler,
    ) -> None:
        def checked(allowed: frozenset[str]) -> Handler:
            resolved = handler(allowed)
            require_async_handler(resolved, what="Method-not-allowed handler")
            return resolved

        self._policy_node(pattern).method_not_allowed_handler = checked
        self._invalidate_lookup()

    def add(
        self,
        route: Route,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        require_async_handler(handler)
        if route is EMPTY_ROUTE:
            raise StarioError("Cannot register the unmatched route")
        if not route.method:
            raise StarioError(
                "Cannot register a prefix route",
                context={"target": route.target},
            )
        method = route.method
        host_segments = route.host_segments
        path_segments = route.path_segments
        tree = self._tree_for(route)
        walk_host = host_segments if tree is self._hosts_param else ()
        scoped_middleware = _collect_middleware(tree, walk_host, path_segments)
        if route.host:
            scoped_middleware.extend(_collect_middleware(self._path, (), path_segments))

        current = self._descend(tree, route)

        wrapped = handler
        for mw in reversed([*scoped_middleware, *middleware]):
            wrapped = mw(wrapped)
        if wrapped is not handler:
            require_async_handler(wrapped, what="Composed handler")

        existing = None if current.endpoints is None else current.endpoints.get(method)
        if existing is not None:
            raise StarioError(
                "Route already registered",
                context={"method": method, "target": route.target},
            )

        endpoint = Endpoint(handler=wrapped, route=route)
        if current.endpoints is None:
            current.endpoints = {method: endpoint}
        else:
            current.endpoints[method] = endpoint
        self._invalidate_lookup()

    @deprecated("Use add(Route(method, path), handler).")
    def handle(
        self,
        method: str,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        """Deprecated. Register with `add(Route(method, path), handler)`."""
        _add_path(self, method, path, handler, middleware)

    @deprecated("Use add(Route('GET', path), handler).")
    def get(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "GET", path, handler, middleware)

    @deprecated("Use add(Route('QUERY', path), handler).")
    def query(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "QUERY", path, handler, middleware)

    @deprecated("Use add(Route('POST', path), handler).")
    def post(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "POST", path, handler, middleware)

    @deprecated("Use add(Route('PUT', path), handler).")
    def put(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "PUT", path, handler, middleware)

    @deprecated("Use add(Route('DELETE', path), handler).")
    def delete(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "DELETE", path, handler, middleware)

    @deprecated("Use add(Route('PATCH', path), handler).")
    def patch(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "PATCH", path, handler, middleware)

    @deprecated("Use add(Route('HEAD', path), handler).")
    def head(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "HEAD", path, handler, middleware)

    @deprecated("Use add(Route('OPTIONS', path), handler).")
    def options(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, "OPTIONS", path, handler, middleware)


def _add_path(
    router: Router,
    method: str,
    path: UrlPath | str,
    handler: Handler,
    middleware: Sequence[Middleware],
) -> None:
    router.add(Route(method, as_target(path)), handler, middleware=middleware)


__all__ = [
    "Router",
    "default_not_found",
    "method_not_allowed_handler",
]
