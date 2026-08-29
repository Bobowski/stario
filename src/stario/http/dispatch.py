"""
Route table: register patterns on a trie, match requests against it.

Host routes are tried first when present; hostless routes are shared defaults (Go-style).
Patterns come from `stario.routing.UrlPath`. Register a `Route` with `add()`.

**Match cache.** `find_handler` memoizes `(host, path, method)` → `(handler, route_match)`
(LRU, 1024 entries). Registration clears the cache. `resolve()` also returns whether
the matched callable is async.

**Sync vs async.** `handler_is_async()` runs when a handler is added — not per request.
Sync handlers run immediately on the protocol thread; async handlers are scheduled as
tasks. Middleware wrapping a sync leaf is adapted so existing `await handler(c, w)`
middleware keeps working; the composed callable is then async.

**404 / 405 policy.** `not_found(pattern, ...)` and `method_not_allowed(pattern, ...)`
attach handlers on the trie branch walked for `pattern`. During a request, the deepest
node along the host/path walk with a policy handler wins (prefix-scoped inheritance).
"""

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache, partial
from types import MappingProxyType
from typing import Literal, cast

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.context import (
    EMPTY_ROUTE_MATCH,
    Context,
    Handler,
    Middleware,
    RouteMatch,
)
from stario.http.writer import Writer
from stario.routing import Route, Segment, UrlPath

type MethodNotAllowedHandler = Callable[[frozenset[str]], Handler]
type MatchStatus = Literal["found", "method_not_allowed", "not_found"]


def _as_pattern(path: UrlPath | str) -> UrlPath:
    return path if isinstance(path, UrlPath) else UrlPath(path)


def handler_is_async(handler: object) -> bool:
    """Return whether `handler` is an async callable. Checked at registration.

    Accepts functions, `functools.partial` of those functions, and callable
    objects whose `__call__` is async. Async generators are rejected — a handler
    writes to `Writer`, it does not yield.
    """
    if not callable(handler):
        raise StarioError(
            "Handler must be callable",
            context={"got": type(handler).__name__},
            help_text=(
                "Register `def handler(c, w):` or `async def handler(c, w):`. "
                "Sync handlers cannot use await; they run as soon as the request is ready."
            ),
        )

    fn: object = handler
    while isinstance(fn, partial):
        fn = fn.func

    if inspect.isasyncgenfunction(fn):
        raise StarioError(
            "Handler cannot be an async generator",
            help_text="Use `async def handler(c, w):` and write to the Writer, not `yield`.",
        )
    if inspect.iscoroutinefunction(fn):
        return True

    call = getattr(fn, "__call__", None)
    if call is not None and call is not fn:
        if inspect.isasyncgenfunction(call):
            raise StarioError(
                "Handler cannot be an async generator",
                help_text="Use `async def __call__(self, c, w):` and write to the Writer, not `yield`.",
            )
        if inspect.iscoroutinefunction(call):
            return True
    return False


def _adapt_sync_handler(handler: Handler) -> Handler:
    """Wrap a sync leaf so async middleware can `await handler(c, w)`."""

    async def adapted(c: Context, w: Writer) -> None:
        handler(c, w)

    adapted.__name__ = getattr(handler, "__name__", "adapted")
    adapted.__qualname__ = getattr(handler, "__qualname__", adapted.__name__)
    adapted.__wrapped__ = handler  # type: ignore[attr-defined]
    return adapted


@dataclass(slots=True)
class Endpoint:
    handler: Handler
    route_match: RouteMatch
    is_async: bool = True


@dataclass(slots=True)
class Node:
    not_found_handler: Handler | None = None
    not_found_is_async: bool = False
    method_not_allowed_handler: MethodNotAllowedHandler | None = None
    middleware: tuple[Middleware, ...] = ()
    host_depth: int = 0
    exact: dict[str, Node] = field(default_factory=lambda: {})
    wildcard_name: str | None = None
    wildcard: Node | None = None
    catchall_name: str | None = None
    catchall: Node | None = None
    endpoints: dict[str, Endpoint] | None = None
    methods: frozenset[str] = frozenset()


def _pattern_child(current: Node, segment: Segment) -> Node | None:
    if segment.kind == "catchall":
        if current.catchall is None or current.catchall_name != segment.name:
            return None
        return current.catchall
    if segment.kind == "wildcard":
        if current.wildcard is None or current.wildcard_name != segment.name:
            return None
        return current.wildcard
    return current.exact.get(segment.name)


def trie_descend(
    root: Node,
    pattern: UrlPath,
    *,
    create: bool,
    host_tree: bool,
) -> Node:
    """Walk a pattern branch on the trie; create child nodes when `create` is true."""
    current = root
    for segment in pattern.host_trie():
        if create:
            current = _descend_or_create(current, segment, host_label=host_tree)
        else:
            child = _pattern_child(current, segment)
            if child is None:
                return current
            current = child
    for segment in pattern.path:
        if create:
            current = _descend_or_create(current, segment, host_label=False)
        else:
            child = _pattern_child(current, segment)
            if child is None:
                return current
            current = child
    return current


def _collect_middleware(
    tree: Node,
    pattern: UrlPath,
    *,
    path_only: bool = False,
) -> list[Middleware]:
    """Collect scope middleware along a registered pattern branch."""
    middlewares: list[Middleware] = list(tree.middleware)
    current = tree

    if not path_only:
        for segment in pattern.host_trie():
            child = _pattern_child(current, segment)
            if child is None:
                return middlewares
            current = child
            middlewares.extend(current.middleware)

    for segment in pattern.path:
        child = _pattern_child(current, segment)
        if child is None:
            break
        current = child
        middlewares.extend(current.middleware)

    return middlewares


def _branch_has_endpoints(node: Node) -> bool:
    if node.endpoints:
        return True
    return (
        (node.wildcard is not None and _branch_has_endpoints(node.wildcard))
        or (node.catchall is not None and _branch_has_endpoints(node.catchall))
        or any(_branch_has_endpoints(child) for child in node.exact.values())
    )


def default_not_found(_c: Context, w: Writer) -> None:
    responses.text(w, "Not Found", 404)


@lru_cache(maxsize=256)
def method_not_allowed_handler(allowed: frozenset[str]) -> Handler:
    allow_header = ", ".join(sorted(allowed))

    def respond(_c: Context, w: Writer) -> None:
        w.headers.set("Allow", allow_header)
        responses.text(w, "Method Not Allowed", 405)

    return respond


type _WalkResult = tuple[
    Node,
    dict[str, str] | None,
    Handler,
    bool,
    bool,
    MethodNotAllowedHandler | None,
]


def _walk_values(
    current: Node,
    segments: tuple[str, ...],
    params: dict[str, str] | None,
    not_found: Handler,
    not_found_custom: bool,
    not_found_is_async: bool,
    method_na: MethodNotAllowedHandler | None,
    *,
    path: str | None = None,
) -> _WalkResult | None:
    """Walk request host or path segments on the trie."""
    path_off = 0 if path is None or path == "/" else 1
    i = 0
    n = len(segments)
    while i < n:
        seg = segments[i]
        child = current.exact.get(seg)
        if child is not None:
            i += 1
            if path is not None:
                path_off += len(seg) + 1
        elif (wc := current.wildcard) is not None:
            if params is None:
                params = {}
            params[cast(str, current.wildcard_name)] = seg
            child = wc
            i += 1
            if path is not None:
                path_off += len(seg) + 1
        elif (ca := current.catchall) is not None:
            if name := current.catchall_name:
                if params is None:
                    params = {}
                if path is None:
                    params[name] = ".".join(reversed(segments[i:]))
                else:
                    params[name] = path[path_off:]
            child = ca
            i = n
        else:
            return None
        if (nf := child.not_found_handler) is not None:
            not_found = nf
            not_found_custom = True
            not_found_is_async = child.not_found_is_async
        if (mna := child.method_not_allowed_handler) is not None:
            method_na = mna
        current = child
    return (
        current,
        params,
        not_found,
        not_found_custom,
        not_found_is_async,
        method_na,
    )


def _resolve(
    root: Node,
    host_segments: tuple[str, ...],
    path: str,
    path_segments: tuple[str, ...],
    method: str,
) -> tuple[Handler, RouteMatch, MatchStatus, bool, bool]:
    params: dict[str, str] | None = None
    if root.not_found_handler is not None:
        not_found = root.not_found_handler
        not_found_is_async = root.not_found_is_async
        not_found_custom = True
    else:
        not_found = default_not_found
        not_found_is_async = False
        not_found_custom = False
    method_na = root.method_not_allowed_handler
    current = root

    walked = _walk_values(
        current,
        host_segments,
        params,
        not_found,
        not_found_custom,
        not_found_is_async,
        method_na,
    )
    if walked is None:
        return (
            not_found,
            EMPTY_ROUTE_MATCH,
            "not_found",
            not_found_custom,
            not_found_is_async,
        )
    current, params, not_found, not_found_custom, not_found_is_async, method_na = walked

    walked = _walk_values(
        current,
        path_segments,
        params,
        not_found,
        not_found_custom,
        not_found_is_async,
        method_na,
        path=path,
    )
    if walked is None:
        return (
            not_found,
            EMPTY_ROUTE_MATCH,
            "not_found",
            not_found_custom,
            not_found_is_async,
        )
    current, params, not_found, not_found_custom, not_found_is_async, method_na = walked

    endpoint = None if current.endpoints is None else current.endpoints.get(method)

    if endpoint is None:
        if current.methods:
            produced = (method_na or method_not_allowed_handler)(current.methods)
            return (
                produced,
                EMPTY_ROUTE_MATCH,
                "method_not_allowed",
                not_found_custom,
                handler_is_async(produced),
            )
        return (
            not_found,
            EMPTY_ROUTE_MATCH,
            "not_found",
            not_found_custom,
            not_found_is_async,
        )

    match = (
        endpoint.route_match
        if params is None
        else RouteMatch(
            pattern=endpoint.route_match.pattern, params=MappingProxyType(params)
        )
    )
    return endpoint.handler, match, "found", not_found_custom, endpoint.is_async


def _descend_or_create(
    current: Node, segment: Segment, *, host_label: bool = False
) -> Node:
    child = _pattern_child(current, segment)
    if child is not None:
        return child

    child_host_depth = current.host_depth + (1 if host_label else 0)

    if segment.kind == "catchall":
        name = segment.name
        if current.catchall is not None:
            raise StarioError(
                "Catchall parameter conflict",
                context={
                    "existing": current.catchall_name,
                    "new": name,
                    "segment": segment.pattern,
                },
                help_text="Use the same catchall parameter name for routes sharing this branch.",
            )
        if current.wildcard is not None:
            raise StarioError(
                "Ambiguous route parameter branch",
                context={
                    "existing": current.wildcard_name,
                    "new": name,
                    "segment": segment.pattern,
                },
                help_text=(
                    "A wildcard and catchall cannot share the same branch because "
                    "matching is deterministic and does not backtrack."
                ),
            )
        child = Node(host_depth=child_host_depth)
        current.catchall_name = name
        current.catchall = child
        return child

    if segment.kind == "wildcard":
        name = segment.name
        if current.wildcard is not None:
            raise StarioError(
                "Wildcard parameter conflict",
                context={
                    "existing": current.wildcard_name,
                    "new": name,
                    "segment": segment.pattern,
                },
                help_text="Use the same wildcard parameter name for routes sharing this branch.",
            )
        if current.catchall is not None:
            raise StarioError(
                "Ambiguous route parameter branch",
                context={
                    "existing": current.catchall_name,
                    "new": name,
                    "segment": segment.pattern,
                },
                help_text=(
                    "A wildcard and catchall cannot share the same branch because "
                    "matching is deterministic and does not backtrack."
                ),
            )
        child = Node(host_depth=child_host_depth)
        current.wildcard_name = name
        current.wildcard = child
        return child

    child = Node(host_depth=child_host_depth)
    current.exact[segment.name] = child
    return child


class Router:
    """Route table: host routes override hostless defaults when they fully match."""

    __slots__ = ("_find_handler", "_has_hosts", "_hosts", "_path")

    def __init__(self) -> None:
        self._path = Node()
        self._hosts = Node()
        self._has_hosts = False

        @lru_cache(maxsize=1024)
        def find_handler(
            host: str,
            path: str,
            method: str,
        ) -> tuple[Handler, RouteMatch, bool]:
            return self._match(
                host,
                path,
                method,
                UrlPath.request_path(path),
                UrlPath.request_host(host),
            )

        self._find_handler = find_handler

    @property
    def host_routing(self) -> bool:
        return self._has_hosts

    def find_handler(
        self,
        host: str,
        path: str,
        method: str,
    ) -> tuple[Handler, RouteMatch]:
        handler, match, _is_async = self._find_handler(host, path, method)
        return handler, match

    def resolve(
        self,
        host: str,
        path: str,
        method: str,
    ) -> tuple[Handler, RouteMatch, bool]:
        """Like `find_handler`, plus whether the matched callable is async."""
        return self._find_handler(host, path, method)

    def _clear_match_cache(self) -> None:
        self._find_handler.cache_clear()

    def _match(
        self,
        host: str,
        path: str,
        method: str,
        path_segments: tuple[str, ...],
        host_labels: tuple[str, ...],
    ) -> tuple[Handler, RouteMatch, bool]:
        if self._has_hosts and host:
            (
                host_handler,
                host_match,
                host_status,
                host_nf_custom,
                host_async,
            ) = _resolve(self._hosts, host_labels, path, path_segments, method)
            if host_status == "found":
                return host_handler, host_match, host_async
            (
                path_handler,
                path_match,
                path_status,
                _path_nf_custom,
                path_async,
            ) = _resolve(self._path, (), path, path_segments, method)
            if path_status == "found":
                return path_handler, path_match, path_async
            if host_status == "method_not_allowed":
                return host_handler, host_match, host_async
            if path_status == "method_not_allowed":
                return path_handler, path_match, path_async
            if host_nf_custom:
                return host_handler, host_match, host_async
            return path_handler, path_match, path_async

        path_handler, path_match, _path_status, _path_nf_custom, path_async = _resolve(
            self._path, (), path, path_segments, method
        )
        return path_handler, path_match, path_async

    def _registration_tree(self, pattern: UrlPath) -> Node:
        if pattern.host:
            self._has_hosts = True
            return self._hosts
        return self._path

    def _leaf_node(self, pattern: UrlPath) -> Node:
        tree = self._registration_tree(pattern)
        return trie_descend(tree, pattern, create=True, host_tree=tree is self._hosts)

    def _policy_node(self, pattern: UrlPath | str) -> Node:
        route = _as_pattern(pattern)
        if any(segment.kind == "catchall" for segment in route.host):
            raise StarioError(
                "Catchall host policy is not supported",
                context={"pattern": route.text},
                help_text=(
                    "Use an exact host prefix such as "
                    "UrlPath('/', host='api.example.com') or apply path policy like '/api'."
                ),
            )
        if route.path and route.path[-1].kind == "catchall":
            raise StarioError(
                "Catchall route policy cannot have child routes",
                context={"pattern": route.text},
                help_text="Use a non-catchall prefix such as '/api' for route policy.",
            )
        return self._leaf_node(route)

    def use(self, pattern: UrlPath | str, *middleware: Middleware) -> None:
        current = self._policy_node(pattern)
        if not middleware:
            return
        if _branch_has_endpoints(current):
            raise StarioError(
                "Middleware must be registered before matching routes",
                context={"pattern": pattern},
                help_text=(
                    "Call app.use(pattern, ...) before app.add(...) or app.get/post on that prefix. "
                    "Middleware is baked into route handlers at registration time."
                ),
            )
        current.middleware = current.middleware + tuple(middleware)
        self._clear_match_cache()

    def not_found(self, pattern: UrlPath | str, handler: Handler) -> None:
        node = self._policy_node(pattern)
        node.not_found_handler = handler
        node.not_found_is_async = handler_is_async(handler)
        self._clear_match_cache()

    def method_not_allowed(
        self,
        pattern: UrlPath | str,
        handler: MethodNotAllowedHandler,
    ) -> None:
        self._policy_node(pattern).method_not_allowed_handler = handler
        self._clear_match_cache()

    def handle(
        self,
        method: str,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        route = _as_pattern(path)
        method = method.upper()
        is_async = handler_is_async(handler)
        tree = self._registration_tree(route)
        scoped_middleware = _collect_middleware(tree, route)
        if route.host:
            scoped_middleware.extend(
                _collect_middleware(self._path, route, path_only=True)
            )

        current = trie_descend(tree, route, create=True, host_tree=tree is self._hosts)

        wrapped = handler
        stack = [*scoped_middleware, *middleware]
        if stack:
            # Existing middleware does `await handler(c, w)`. Adapt a sync leaf
            # so that contract still holds; the composed callable is async.
            if not is_async:
                wrapped = _adapt_sync_handler(wrapped)
            for mw in reversed(stack):
                wrapped = mw(wrapped)
            is_async = handler_is_async(wrapped)

        existing = None if current.endpoints is None else current.endpoints.get(method)
        if existing is not None:
            raise StarioError(
                "Route already registered",
                context={"method": method, "pattern": route.text},
                help_text="Each HTTP method may be registered only once per route pattern.",
            )

        endpoint = Endpoint(
            handler=wrapped,
            route_match=RouteMatch(
                pattern=route.text,
                params=EMPTY_ROUTE_MATCH.params,
            ),
            is_async=is_async,
        )
        if current.endpoints is None:
            current.endpoints = {method: endpoint}
        else:
            current.endpoints[method] = endpoint
        if method not in current.methods:
            current.methods = current.methods | frozenset({method})
        self._clear_match_cache()

    def add(
        self,
        route: Route,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        """Register a `Route`. Path + method stay on `handle()`."""
        if not isinstance(route, Route):
            raise StarioError(
                "add() takes a Route",
                context={"got": type(route).__name__},
                help_text=(
                    "Declare the endpoint with Route.get/post/... and pass that object. "
                    "Use app.handle(method, path, handler) or app.get(path, handler) "
                    "for a path without a Route."
                ),
            )
        self.handle(route.method, route.path, handler, middleware=middleware)

    def get(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("GET", path, handler, middleware=middleware)

    def query(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("QUERY", path, handler, middleware=middleware)

    def post(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("POST", path, handler, middleware=middleware)

    def put(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("PUT", path, handler, middleware=middleware)

    def delete(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("DELETE", path, handler, middleware=middleware)

    def patch(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("PATCH", path, handler, middleware=middleware)

    def head(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("HEAD", path, handler, middleware=middleware)

    def options(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("OPTIONS", path, handler, middleware=middleware)


__all__ = [
    "Router",
    "default_not_found",
    "handler_is_async",
    "method_not_allowed_handler",
]
