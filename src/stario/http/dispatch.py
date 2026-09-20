"""Route table: register `Route` values on a trie and resolve `(host, path, method)`."""

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache, partial
from typing import Literal
from warnings import deprecated

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.context import EMPTY_MATCH, Context, Handler, Match, Middleware
from stario.http.route import EMPTY_ROUTE, HTTP_METHODS, Route, UrlPath, as_target
from stario.http.segment import Segment
from stario.http.writer import Writer

type MethodNotAllowedHandler = Callable[[frozenset[str]], Handler]
type MatchStatus = Literal["found", "method_not_allowed", "not_found"]
type RouteMatch = tuple[Handler, Route, Match]
type Resolve = tuple[RouteMatch, MatchStatus, bool]


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
    exact: dict[str, Node] = field(default_factory=dict)
    rest: dict[str, str] | None = None
    wildcard_name: str | None = None
    wildcard: Node | None = None
    catchall_name: str | None = None
    catchall: Node | None = None
    endpoints: dict[str, Endpoint] | None = None


class _Cur:
    __slots__ = ("method_na", "node", "not_found", "not_found_custom", "params")

    def __init__(self, root: Node) -> None:
        self.node = root
        self.params: dict[str, str] | None = None
        self.not_found = root.not_found_handler or default_not_found
        self.not_found_custom = root.not_found_handler is not None
        self.method_na = root.method_not_allowed_handler


def _edge_rest(node: Node, key: str) -> str:
    rest = node.rest
    if rest is None:
        return ""
    return rest.get(key, "")


def _set_rest(node: Node, key: str, extra: str) -> None:
    if extra:
        if node.rest is None:
            node.rest = {}
        node.rest[key] = extra
    elif node.rest is not None:
        node.rest.pop(key, None)


def _compress(node: Node, sep: str) -> None:
    for child in node.exact.values():
        _compress(child, sep)
    if node.wildcard is not None:
        _compress(node.wildcard, sep)
    if node.catchall is not None:
        _compress(node.catchall, sep)
    while (
        node.wildcard is None
        and node.catchall is None
        and len(node.exact) == 1
    ):
        key, child = next(iter(node.exact.items()))
        if (
            child.endpoints
            or child.middleware
            or child.not_found_handler is not None
            or child.method_not_allowed_handler is not None
            or child.wildcard is not None
            or child.catchall is not None
            or len(child.exact) != 1
        ):
            break
        ck, grandchild = next(iter(child.exact.items()))
        extra = sep.join(p for p in (_edge_rest(node, key), ck, _edge_rest(child, ck)) if p)
        node.exact[key] = grandchild
        _set_rest(node, key, extra)


def _param_child(current: Node, segment: Segment) -> Node | None:
    if segment.kind == "catchall":
        if current.catchall is None or current.catchall_name != segment.name:
            return None
        return current.catchall
    if current.wildcard is None or current.wildcard_name != segment.name:
        return None
    return current.wildcard


def _advance_exact(
    current: Node, segs: tuple[Segment, ...], i: int, sep: str
) -> tuple[Node, int] | None:
    seg = segs[i]
    child = current.exact.get(seg.name)
    if child is None:
        return None
    extra = _edge_rest(current, seg.name)
    if not extra:
        return child, i + 1
    parts = extra.split(sep)
    for offset, part in enumerate(parts):
        nxt = i + 1 + offset
        if nxt >= len(segs) or segs[nxt].kind != "exact" or segs[nxt].name != part:
            return None
    return child, i + 1 + len(parts)


def _collect_middleware(
    tree: Node,
    host_segments: tuple[Segment, ...],
    path_segments: tuple[Segment, ...],
) -> list[Middleware]:
    middlewares: list[Middleware] = list(tree.middleware)
    current = tree

    def follow(segs: tuple[Segment, ...], sep: str, *, halt: bool) -> bool:
        nonlocal current
        i = 0
        while i < len(segs):
            seg = segs[i]
            if seg.kind == "exact":
                stepped = _advance_exact(current, segs, i, sep)
            else:
                child = _param_child(current, seg)
                stepped = (child, i + 1) if child is not None else None
            if stepped is None:
                return halt
            current, i = stepped
            middlewares.extend(current.middleware)
        return False

    if follow(host_segments[::-1], ".", halt=True):
        return middlewares
    follow(path_segments, "/", halt=False)
    return middlewares


def _branch_has_endpoints(node: Node) -> bool:
    if node.endpoints:
        return True
    return (
        (node.wildcard is not None and _branch_has_endpoints(node.wildcard))
        or (node.catchall is not None and _branch_has_endpoints(node.catchall))
        or any(_branch_has_endpoints(child) for child in node.exact.values())
    )


def _set_param(cur: _Cur, name: str, value: str) -> None:
    params = cur.params
    if params is None:
        params = {}
        cur.params = params
    params[name] = value


def _take_param(cur: _Cur, current: Node, seg: str, rest: str | None) -> Node | None:
    if (wc := current.wildcard) is not None and (name := current.wildcard_name):
        _set_param(cur, name, seg)
        return wc
    if (ca := current.catchall) is not None:
        if name := current.catchall_name:
            _set_param(cur, name, rest if rest is not None else seg)
        return ca
    return None


def _enter(cur: _Cur, child: Node) -> None:
    if (nf := child.not_found_handler) is not None:
        cur.not_found = nf
        cur.not_found_custom = True
    if (mna := child.method_not_allowed_handler) is not None:
        cur.method_na = mna
    cur.node = child


def _walk_path(cur: _Cur, path: str) -> bool:
    if path == "/":
        return True
    n = len(path)
    i = 1
    while i <= n:
        if i == n:
            if path[n - 1] != "/":
                break
            seg = ""
            end = n
            nxt = n + 1
        else:
            slash = path.find("/", i)
            end = n if slash < 0 else slash
            seg = path[i:end]
            nxt = end + 1
        node = cur.node
        child = node.exact.get(seg)
        if child is not None:
            extra = _edge_rest(node, seg)
            if extra:
                after = end + 1
                bound = after + len(extra)
                if (
                    end >= n
                    or path[end] != "/"
                    or not path.startswith(extra, after)
                    or (bound < n and path[bound] != "/")
                ):
                    child = None
                else:
                    nxt = bound + 1 if bound < n else bound
        if child is None:
            rest = path[i:] if node.catchall is not None else None
            child = _take_param(cur, node, seg, rest)
            if child is None:
                return False
            if child is node.catchall:
                nxt = n + 1
        _enter(cur, child)
        i = nxt
    return True


def _walk_host(cur: _Cur, host: str) -> bool:
    if not host:
        return True
    end = len(host)
    while end > 0:
        dot = host.rfind(".", 0, end)
        seg = host[dot + 1 : end]
        node = cur.node
        child = node.exact.get(seg)
        if child is None:
            rest = host[:end] if node.catchall is not None else None
            child = _take_param(cur, node, seg, rest)
            if child is None:
                return False
            end = 0 if child is node.catchall else (dot if dot >= 0 else 0)
        else:
            end = dot if dot >= 0 else 0
        _enter(cur, child)
    return True


def _miss(handler: Handler, custom: bool) -> Resolve:
    return (handler, EMPTY_ROUTE, EMPTY_MATCH), "not_found", custom


def _finish(cur: _Cur, method: str) -> Resolve:
    node = cur.node
    endpoints = node.endpoints
    endpoint = None if endpoints is None else endpoints.get(method)
    if endpoint is None:
        if endpoints:
            return (
                (
                    (cur.method_na or method_not_allowed_handler)(frozenset(endpoints)),
                    EMPTY_ROUTE,
                    EMPTY_MATCH,
                ),
                "method_not_allowed",
                cur.not_found_custom,
            )
        return _miss(cur.not_found, cur.not_found_custom)
    route = endpoint.route
    params = cur.params
    hit = Match(route.pattern, params) if params else Match(route.pattern)
    return (endpoint.handler, route, hit), "found", cur.not_found_custom


def _resolve(root: Node, path: str, method: str, host: str = "") -> Resolve:
    cur = _Cur(root)
    if host:
        nf, custom = cur.not_found, cur.not_found_custom
        if not _walk_host(cur, host):
            return _miss(nf, custom)
    nf, custom = cur.not_found, cur.not_found_custom
    if not _walk_path(cur, path):
        return _miss(nf, custom)
    return _finish(cur, method)


_NOT_FOUND: Resolve = _miss(default_not_found, False)


def _open_param(current: Node, segment: Segment) -> Node:
    child = _param_child(current, segment)
    if child is not None:
        return child
    catchall = segment.kind == "catchall"
    taken = current.catchall_name if catchall else current.wildcard_name
    other = current.wildcard if catchall else current.catchall
    if taken is not None:
        raise StarioError(
            "Catchall parameter conflict" if catchall else "Wildcard parameter conflict",
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
                "existing": current.wildcard_name if catchall else current.catchall_name,
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


def _descend_or_create(current: Node, segment: Segment, sep: str) -> Node:
    if segment.kind != "exact":
        return _open_param(current, segment)

    name = segment.name
    child = current.exact.get(name)
    if child is None:
        child = Node()
        current.exact[name] = child
        return child
    extra = _edge_rest(current, name)
    if not extra:
        return child
    first, _, after = extra.partition(sep)
    mid = Node()
    mid.exact[first] = child
    _set_rest(mid, first, after)
    current.exact[name] = mid
    _set_rest(current, name, "")
    return mid


class Router:
    """Route table: host routes override hostless defaults when they fully match."""

    __slots__ = (
        "_exact",
        "_has_param_hosts",
        "_host_routing",
        "_hosts_exact",
        "_hosts_param",
        "_lookup",
        "_path",
    )

    def __init__(self) -> None:
        self._path = Node()
        self._hosts_exact: dict[str, Node] = {}
        self._hosts_param = Node()
        self._has_param_hosts = False
        self._host_routing = False
        self._exact: dict[tuple[str, str, str], RouteMatch] = {}

        @lru_cache(maxsize=1024)
        def lookup(host: str, path: str, method: str) -> RouteMatch:
            return self._resolve_handler(host, path, method)

        self._lookup = lookup

    @property
    def host_routing(self) -> bool:
        return self._host_routing

    def find_handler(self, host: str, path: str, method: str) -> RouteMatch:
        """`host` must already be lowercased (`Request.host`)."""
        return self._lookup(host, path, method)

    def _invalidate_lookup(self) -> None:
        self._lookup.cache_clear()

    def _resolve_handler(self, host: str, path: str, method: str) -> RouteMatch:
        """Static exact map, then trie. Cached by `find_handler`."""
        hit = self._exact.get((host, path, method))
        if hit is not None:
            return hit

        # Host tree first. On miss: hostless exact, hostless walk,
        # then host 405, path 405, host custom 404, path 404.
        if self._host_routing and host:
            hroot = self._hosts_exact.get(host)
            if hroot is not None:
                host_hit, host_status, host_custom = _resolve(hroot, path, method)
            elif self._has_param_hosts:
                host_hit, host_status, host_custom = _resolve(
                    self._hosts_param, path, method, host
                )
            else:
                host_hit, host_status, host_custom = _NOT_FOUND
            if host_status == "found":
                return host_hit

            hit = self._exact.get(("", path, method))
            if hit is not None:
                return hit
            path_hit, path_status, _path_custom = _resolve(self._path, path, method)
            if path_status == "found":
                return path_hit
            if host_status == "method_not_allowed":
                return host_hit
            if path_status == "method_not_allowed":
                return path_hit
            if host_custom:
                return host_hit
            return path_hit

        return _resolve(self._path, path, method)[0]

    def _tree_for(self, route: Route) -> Node:
        if not route.host:
            return self._path
        self._host_routing = True
        if all(segment.kind == "exact" for segment in route._host_segments):
            return self._hosts_exact.setdefault(route.host, Node())
        self._has_param_hosts = True
        return self._hosts_param

    def _descend(self, tree: Node, route: Route) -> Node:
        current = tree
        if tree is self._hosts_param:
            for segment in route._host_segments[::-1]:
                current = _descend_or_create(current, segment, ".")
        for segment in route._path_segments:
            current = _descend_or_create(current, segment, "/")
        return current

    def _policy_node(self, pattern: UrlPath | str) -> Node:
        route = Route(as_target(pattern))
        if any(segment.kind == "catchall" for segment in route._host_segments):
            raise StarioError(
                "Catchall host policy is not supported",
                context={"target": route.target},
            )
        if route._path_segments and route._path_segments[-1].kind == "catchall":
            raise StarioError(
                "Catchall route policy cannot have child routes",
                context={"target": route.target},
            )
        tree = self._tree_for(route)
        current = self._descend(tree, route)
        if tree is not self._hosts_param:
            _compress(tree, "/")
        return current

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
        host_segments = route._host_segments
        path_segments = route._path_segments
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

        if route._href is None:
            key = (
                "/" + "/".join(segment.name for segment in path_segments)
                if path_segments
                else "/"
            )
            self._exact[(route.host or "", key, method)] = (
                wrapped,
                route,
                Match(route.pattern),
            )
        if tree is not self._hosts_param:
            _compress(tree, "/")
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


def _add_path(
    router: Router,
    method: str,
    path: UrlPath | str,
    handler: Handler,
    middleware: Sequence[Middleware],
) -> None:
    router.add(Route(method, as_target(path)), handler, middleware=middleware)


def _router_verb(method: str):
    @deprecated(f"Use add(Route({method!r}, path), handler).")
    def verb(
        self: Router,
        path: UrlPath | str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        _add_path(self, method, path, handler, middleware)

    verb.__name__ = method.lower()
    verb.__qualname__ = f"Router.{method.lower()}"
    return verb


for _method in HTTP_METHODS:
    setattr(Router, _method.lower(), _router_verb(_method))


__all__ = [
    "Router",
    "default_not_found",
    "method_not_allowed_handler",
]
