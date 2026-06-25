"""
Route table: register patterns on a trie, match requests against it.

Host routes are tried first when present; hostless routes are shared defaults (Go-style).
Patterns come from `stario.routing.UrlPath`; build URLs there, not here.

**Match cache.** `find_handler` memoizes `(host, path, method)` → `(handler, route_match)`
(LRU, 1024 entries). Registration clears the cache.

**404 / 405 policy.** `not_found(pattern, ...)` and `method_not_allowed(pattern, ...)`
attach handlers on the trie branch walked for `pattern`. During a request, the deepest
node along the host/path walk with a policy handler wins (prefix-scoped inheritance).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
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
from stario.routing import Segment, UrlPath

type MethodNotAllowedHandler = Callable[[frozenset[str]], Handler]
type MatchStatus = Literal["found", "method_not_allowed", "not_found"]


def _as_pattern(path: UrlPath | str) -> UrlPath:
    return path if isinstance(path, UrlPath) else UrlPath(path)


@dataclass(slots=True)
class Endpoint:
    handler: Handler
    route_match: RouteMatch


@dataclass(slots=True)
class Node:
    not_found_handler: Handler | None = None
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


async def default_not_found(_c: Context, w: Writer) -> None:
    responses.text(w, "Not Found", 404)


@lru_cache(maxsize=256)
def method_not_allowed_handler(allowed: frozenset[str]) -> Handler:
    allow_header = ", ".join(sorted(allowed))

    async def respond(_c: Context, w: Writer) -> None:
        w.headers.set("Allow", allow_header)
        responses.text(w, "Method Not Allowed", 405)

    return respond


def _walk_values(
    current: Node,
    segments: tuple[str, ...],
    params: dict[str, str] | None,
    not_found: Handler,
    not_found_custom: bool,
    method_na: MethodNotAllowedHandler | None,
    *,
    path: str | None = None,
) -> (
    tuple[Node, dict[str, str] | None, Handler, bool, MethodNotAllowedHandler | None]
    | None
):
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
        if (mna := child.method_not_allowed_handler) is not None:
            method_na = mna
        current = child
    return current, params, not_found, not_found_custom, method_na


def _resolve(
    root: Node,
    host_segments: tuple[str, ...],
    path: str,
    path_segments: tuple[str, ...],
    method: str,
) -> tuple[Handler, RouteMatch, MatchStatus, bool]:
    params: dict[str, str] | None = None
    not_found = root.not_found_handler or default_not_found
    not_found_custom = root.not_found_handler is not None
    method_na = root.method_not_allowed_handler
    current = root

    walked = _walk_values(
        current,
        host_segments,
        params,
        not_found,
        not_found_custom,
        method_na,
    )
    if walked is None:
        return not_found, EMPTY_ROUTE_MATCH, "not_found", not_found_custom
    current, params, not_found, not_found_custom, method_na = walked

    walked = _walk_values(
        current,
        path_segments,
        params,
        not_found,
        not_found_custom,
        method_na,
        path=path,
    )
    if walked is None:
        return not_found, EMPTY_ROUTE_MATCH, "not_found", not_found_custom
    current, params, not_found, not_found_custom, method_na = walked

    endpoint = None if current.endpoints is None else current.endpoints.get(method)

    if endpoint is None:
        if current.methods:
            return (
                (method_na or method_not_allowed_handler)(current.methods),
                EMPTY_ROUTE_MATCH,
                "method_not_allowed",
                not_found_custom,
            )
        return not_found, EMPTY_ROUTE_MATCH, "not_found", not_found_custom

    if params is None:
        return endpoint.handler, endpoint.route_match, "found", not_found_custom
    return (
        endpoint.handler,
        RouteMatch(
            pattern=endpoint.route_match.pattern, params=MappingProxyType(params)
        ),
        "found",
        not_found_custom,
    )


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
        ) -> tuple[Handler, RouteMatch]:
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
    ) -> tuple[Handler, RouteMatch]:
        if self._has_hosts and host:
            host_handler, host_match, host_status, host_nf_custom = _resolve(
                self._hosts, host_labels, path, path_segments, method
            )
            if host_status == "found":
                return host_handler, host_match
            path_handler, path_match, path_status, _path_nf_custom = _resolve(
                self._path, (), path, path_segments, method
            )
            if path_status == "found":
                return path_handler, path_match
            if host_status == "method_not_allowed":
                return host_handler, host_match
            if path_status == "method_not_allowed":
                return path_handler, path_match
            if host_nf_custom:
                return host_handler, host_match
            return path_handler, path_match

        path_handler, path_match, path_status, _path_nf_custom = _resolve(
            self._path, (), path, path_segments, method
        )
        return path_handler, path_match

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
                    "Call app.use(pattern, ...) before app.get/post/etc. "
                    "Middleware is baked into route handlers at registration time."
                ),
            )
        current.middleware = current.middleware + tuple(middleware)
        self._clear_match_cache()

    def not_found(self, pattern: UrlPath | str, handler: Handler) -> None:
        self._policy_node(pattern).not_found_handler = handler
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
        tree = self._registration_tree(route)
        scoped_middleware = _collect_middleware(tree, route)
        if route.host:
            scoped_middleware.extend(
                _collect_middleware(self._path, route, path_only=True)
            )

        current = trie_descend(tree, route, create=True, host_tree=tree is self._hosts)

        wrapped = handler
        for mw in reversed([*scoped_middleware, *middleware]):
            wrapped = mw(wrapped)

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
        )
        if current.endpoints is None:
            current.endpoints = {method: endpoint}
        else:
            current.endpoints[method] = endpoint
        if method not in current.methods:
            current.methods = current.methods | frozenset({method})
        self._clear_match_cache()

    def get(
        self,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        self.handle("GET", path, handler, middleware=middleware)

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
    "method_not_allowed_handler",
]
