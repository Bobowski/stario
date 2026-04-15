"""
URL registration (no decorators): the route table is exactly what you registered.

Matching is a segment trie with an optional host prefix, then path segments, then HTTP method. ``name=`` on a route
feeds ``named_routes`` so ``App.url_for`` can build URLs from logical names.

Mounting merges child tries and name tables into the parent; ``App.__call__`` resolves each request against
the combined tree.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Literal, Protocol

import stario.responses as responses
from stario.exceptions import StarioError, StarioRuntime

from .context import EMPTY_ROUTE_MATCH, Context, Handler, Middleware, RouteMatch
from .writer import Writer


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    path = "/" + path.strip("/")
    return path if path != "/" else "/"


def _split_path_segments(path: str) -> list[str]:
    path = _normalize_path(path)
    segments = path[1:].split("/")
    for seg in segments[:-1]:
        if seg.startswith("{") and seg.endswith("...}"):
            raise StarioError(
                "Catchall path param in invalid position",
                context={"path": path, "segment": seg},
                help_text="Move the catchall to the end of the path.",
            )
    return segments


def _split_pattern(pattern: str) -> tuple[list[str], list[str]]:
    if "/" not in pattern:
        expected = f"/{pattern.strip('/')}"
        if "." in pattern and pattern != ".":
            expected = f"{pattern.rstrip('/')}/"
        raise StarioError(
            "Pattern must include a path starting with '/'",
            context={"pattern": pattern},
            help_text=(
                f"Expected '{expected}'. "
                "Use '/path' for path-only routes or '[host]/[path]' for host-aware routes."
            ),
        )

    host, path = pattern.split("/", 1)
    host_segments = host.lower().split(".") if host else []
    for seg in host_segments[1:]:
        if seg.startswith("{") and seg.endswith("...}"):
            raise StarioError(
                "Catchall host param in invalid position",
                context={"host": host, "segment": seg},
                help_text="Move the catchall to the first segment of the host.",
            )
    return host_segments, _split_path_segments("/" + path)


@dataclass(slots=True, frozen=True)
class Node:
    kind: Literal["host", "path", "method"]
    not_found_handler: Handler
    route_handler: Handler | None = None
    exact: dict[str, Node] = field(default_factory=dict)
    wildcard: tuple[str, Node] | None = None
    catchall: tuple[str, Node] | None = None


_EMPTY_NAMED_ROUTES = MappingProxyType({})


class Subtree(Protocol):
    root: Node

    @property
    def named_routes(self) -> Mapping[str, str]: ...


def _wrap_path_segments(
    child: Node,
    segments: list[str],
    not_found_handler: Handler,
) -> Node:
    for seg in reversed(segments):
        if seg.startswith("{") and seg.endswith("...}"):
            child = Node("path", not_found_handler, catchall=(seg[1:-4], child))
        elif seg.startswith("{") and seg.endswith("}"):
            child = Node("path", not_found_handler, wildcard=(seg[1:-1], child))
        else:
            child = Node("path", not_found_handler, exact={seg: child})
    return child


def _wrap_host_segments(
    child: Node,
    segments: list[str],
    not_found_handler: Handler,
) -> Node:
    for seg in segments:
        if seg.startswith("{") and seg.endswith("...}"):
            child = Node("host", not_found_handler, catchall=(seg[1:-4], child))
        elif seg.startswith("{") and seg.endswith("}"):
            child = Node("host", not_found_handler, wildcard=(seg[1:-1], child))
        else:
            child = Node("host", not_found_handler, exact={seg.lower(): child})
    return child


def _prefix_path_tree(
    tree: Node,
    prefix: list[str],
    not_found_handler: Handler,
) -> Node:
    if not prefix:
        return tree
    if tree.kind == "path":
        return _wrap_path_segments(tree, prefix, not_found_handler)
    if tree.kind != "host":
        raise StarioError(
            "Cannot prefix a method node",
            context={"tree": tree},
            help_text="Mount prefixes can only be inserted above path nodes.",
        )
    return Node(
        kind="host",
        route_handler=tree.route_handler,
        not_found_handler=tree.not_found_handler,
        exact={
            key: _prefix_path_tree(child, prefix, not_found_handler)
            for key, child in tree.exact.items()
        },
        wildcard=(
            None
            if tree.wildcard is None
            else (
                tree.wildcard[0],
                _prefix_path_tree(tree.wildcard[1], prefix, not_found_handler),
            )
        ),
        catchall=(
            None
            if tree.catchall is None
            else (
                tree.catchall[0],
                _prefix_path_tree(tree.catchall[1], prefix, not_found_handler),
            )
        ),
    )


def _merge_trees(tree1: Node, tree2: Node) -> Node:
    if tree1.kind != tree2.kind:
        if {tree1.kind, tree2.kind} == {"host", "path"}:
            if tree1.kind == "path":
                tree1 = Node("host", tree1.not_found_handler, catchall=("", tree1))
            if tree2.kind == "path":
                tree2 = Node("host", tree2.not_found_handler, catchall=("", tree2))
            return _merge_trees(tree1, tree2)
        raise StarioError(
            "Nodes have conflicting kinds",
            context={"tree1": tree1, "tree2": tree2},
            help_text="Only path and host nodes can be promoted during merge.",
        )

    if (
        tree1.route_handler is not None
        and tree2.route_handler is not None
        and tree1.route_handler is not tree2.route_handler
    ):
        raise StarioError(
            "Nodes have conflicting handlers",
            context={"tree1": tree1, "tree2": tree2},
            help_text="Nodes have conflicting handlers.",
        )

    if tree1.wildcard is not None and tree2.wildcard is not None:
        if tree1.wildcard[0] != tree2.wildcard[0]:
            raise StarioError(
                "Nodes have conflicting wildcards",
                context={
                    "tree1": tree1,
                    "tree2": tree2,
                    "name1": tree1.wildcard[0],
                    "name2": tree2.wildcard[0],
                },
                help_text="Wildcard node names must match when merging trees.",
            )
        wildcard = (
            tree1.wildcard[0],
            _merge_trees(tree1.wildcard[1], tree2.wildcard[1]),
        )
    else:
        wildcard = tree1.wildcard or tree2.wildcard

    if tree1.catchall is not None and tree2.catchall is not None:
        if tree1.catchall[0] != tree2.catchall[0]:
            raise StarioError(
                "Nodes have conflicting catchalls",
                context={
                    "tree1": tree1,
                    "tree2": tree2,
                    "name1": tree1.catchall[0],
                    "name2": tree2.catchall[0],
                },
                help_text="Catchall node names must match when merging trees.",
            )
        catchall = (
            tree1.catchall[0],
            _merge_trees(tree1.catchall[1], tree2.catchall[1]),
        )
    else:
        catchall = tree1.catchall or tree2.catchall

    tree1_keys = set(tree1.exact)
    tree2_keys = set(tree2.exact)
    exact = (
        {k: tree1.exact[k] for k in tree1_keys.difference(tree2_keys)}
        | {k: tree2.exact[k] for k in tree2_keys.difference(tree1_keys)}
        | {
            k: _merge_trees(tree1.exact[k], tree2.exact[k])
            for k in tree1_keys.intersection(tree2_keys)
        }
    )

    return Node(
        kind=tree1.kind,
        route_handler=tree1.route_handler or tree2.route_handler,
        not_found_handler=tree1.not_found_handler or tree2.not_found_handler,
        exact=exact,
        wildcard=wildcard,
        catchall=catchall,
    )


def _apply_middlewares(tree: Node, middlewares: tuple[Middleware, ...]) -> Node:
    if not middlewares:
        return tree

    route_handler = tree.route_handler
    if route_handler is not None:
        for mw in middlewares:
            route_handler = mw(route_handler)

    return Node(
        kind=tree.kind,
        route_handler=route_handler,
        not_found_handler=tree.not_found_handler,
        exact={
            key: _apply_middlewares(child, middlewares)
            for key, child in tree.exact.items()
        },
        wildcard=(
            None
            if tree.wildcard is None
            else (tree.wildcard[0], _apply_middlewares(tree.wildcard[1], middlewares))
        ),
        catchall=(
            None
            if tree.catchall is None
            else (tree.catchall[0], _apply_middlewares(tree.catchall[1], middlewares))
        ),
    )


async def default_not_found(_c: Context, w: Writer) -> None:
    """Default 404 handler when no route matches (override via ``Router(..., not_found_handler=...)``)."""
    responses.text(w, "Not Found", 404)


@lru_cache(maxsize=256)
def method_not_allowed_handler(allowed: frozenset[str]) -> Handler:
    allow_header = ", ".join(sorted(allowed))

    async def respond(_c: Context, w: Writer) -> None:
        w.headers.set("Allow", allow_header)
        responses.text(w, "Method Not Allowed", 405)

    return respond


def _prefix_named_pattern(
    pattern: str,
    *,
    mount_pattern: str,
    host_segments: list[str],
    path_segments: list[str],
) -> str:
    child_host_segments, child_path_segments = _split_pattern(pattern)
    if host_segments and child_host_segments:
        raise StarioError(
            "Host matching already defined",
            context={"prefix": mount_pattern, "pattern": pattern},
            help_text="Define host matching either on the mounted subtree or in mount(), not both.",
        )
    prefix_path = "/" + "/".join(path_segments)
    child_path = "/".join(child_path_segments)
    prefixed_path = _normalize_path(f"{prefix_path.rstrip('/')}/{child_path}")
    combined_host_segments = host_segments or child_host_segments
    return (
        f"{'.'.join(combined_host_segments)}{prefixed_path}"
        if combined_host_segments
        else prefixed_path
    )


def find_handler(
    host: str, path: str, method: str, tree: Node
) -> tuple[Handler, RouteMatch]:
    """Walk ``tree`` for ``host``, ``path``, and ``method``; return handler and match (or 404/405 handler)."""
    host_segments = list(reversed(host.lower().split("."))) if host else []
    path = _normalize_path(path)
    path_segments = path[1:].split("/") if path != "/" else [""]
    current = tree
    started_with_host = current.kind == "host"
    params: dict[str, str] = {}
    host_parts: list[str] = []
    path_parts: list[str] = []

    host_index = 0
    while current.kind == "host":
        if host_index >= len(host_segments):
            if current.catchall is not None and current.catchall[0] == "":
                current = current.catchall[1]
                continue
            return current.not_found_handler, EMPTY_ROUTE_MATCH

        seg = host_segments[host_index]
        child = current.exact.get(seg)
        if child is not None:
            current = child
            host_parts.append(seg)
            host_index += 1
            continue

        if current.wildcard is not None:
            name, child = current.wildcard
            current = child
            params[name] = seg
            host_parts.append("{" + name + "}")
            host_index += 1
            continue

        if current.catchall is not None:
            name, child = current.catchall
            if name:
                params[name] = ".".join(reversed(host_segments[host_index:]))
                host_parts.append("{" + name + "...}")
            current = child
            host_index = len(host_segments)
            continue

        return current.not_found_handler, EMPTY_ROUTE_MATCH

    if started_with_host and host_index != len(host_segments):
        return current.not_found_handler, EMPTY_ROUTE_MATCH

    path_index = 0
    while path_index < len(path_segments):
        seg = path_segments[path_index]
        child = current.exact.get(seg)
        if child is not None:
            current = child
            path_parts.append(seg)
            path_index += 1
            continue

        if current.wildcard is not None:
            name, child = current.wildcard
            current = child
            params[name] = seg
            path_parts.append("{" + name + "}")
            path_index += 1
            continue

        if current.catchall is not None:
            name, child = current.catchall
            current = child
            params[name] = "/".join(path_segments[path_index:])
            path_parts.append("{" + name + "...}")
            path_index = len(path_segments)
            continue

        return current.not_found_handler, EMPTY_ROUTE_MATCH

    leaf = current.exact.get(method)
    if leaf is None:
        empty_child = current.exact.get("")
        if empty_child is not None and empty_child.kind == "path":
            current = empty_child
            leaf = current.exact.get(method)

    if leaf is None:
        allowed = frozenset(m for m, c in current.exact.items() if c.kind == "method")
        if allowed:
            return method_not_allowed_handler(allowed), EMPTY_ROUTE_MATCH
        return current.not_found_handler, EMPTY_ROUTE_MATCH

    host_pattern = ".".join(reversed(host_parts))
    path_pattern = "/".join(path_parts)
    pattern = f"{host_pattern}/{path_pattern}" if host_pattern else f"/{path_pattern}"
    if leaf.route_handler is None:
        raise StarioRuntime(
            "Route trie invariant violated: method leaf without handler",
            context={"method": method, "pattern": pattern},
            help_text="This indicates an internal bug in route registration.",
        )
    return leaf.route_handler, RouteMatch(
        pattern=pattern, params=MappingProxyType(params)
    )


class Router:
    """Explicit route table: register methods and paths, optionally mount subtrees into one trie.

    The live server invokes ``App.__call__``, which walks this table.
    Do not treat ``Router`` as the HTTP entrypoint—call ``await app(c, w)`` on an ``App`` instance.
    """

    __slots__ = ("_middlewares", "_not_found_handler", "named_routes", "root")

    def __init__(
        self,
        *,
        middleware: Sequence[Middleware] = (),
        not_found_handler: Handler | None = None,
    ) -> None:
        """Create an empty trie rooted at ``/``.

        Parameters:
            middleware: Wrappers for this router: applied around every handler registered on this trie (registration order: last listed runs first on the inbound request).
            not_found_handler: Called when no route matches; omit for the framework default (plain ``404`` text).
        """
        self._middlewares = tuple(middleware)
        self._not_found_handler = (
            default_not_found if not_found_handler is None else not_found_handler
        )
        self.named_routes: Mapping[str, str] = _EMPTY_NAMED_ROUTES
        self.root = Node(kind="path", not_found_handler=self._not_found_handler)

    def push_middleware(self, *middleware: Middleware) -> None:
        """Append middleware and re-wrap all handlers already on the trie (in addition to future registrations).

        Parameters:
            middleware: One or more ``(inner) -> outer`` wrappers; each new piece sits outermost on the existing stack.
        """
        if not middleware:
            return
        self._middlewares = self._middlewares + tuple(middleware)
        self.root = _apply_middlewares(self.root, tuple(middleware))
        self._find_handler.cache_clear()

    @lru_cache(maxsize=1024)
    def _find_handler(
        self, host: str, path: str, method: str
    ) -> tuple[Handler, RouteMatch]:
        return find_handler(host, path, method, self.root)

    def handle(
        self,
        method: str,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Attach a handler to one HTTP method and pattern.

        Parameters:
            method: Verb such as ``GET`` or ``POST`` (case as you prefer; compared as given).
            pattern: ``/path`` with optional ``{id}`` segments, or ``host.name/path`` for host-based routes.
            handler: Async ``(context, writer) -> None`` invoked when this route wins.
            middleware: Extra wrappers for this route only (after this router’s ``_middlewares`` stack).
            name: If set, registers a reverse-lookup key for ``App.url_for``.

        Raises:
            StarioError: On duplicate ``name`` or invalid pattern text.

        Notes:
            Catch-all segments use ``{name...}`` and must be last on the path (or first on the host).
        """
        if name is not None and name in self.named_routes:
            raise StarioError(
                "Name already registered",
                context={"name": name, "pattern": pattern},
                help_text="Use a different name to avoid conflicts.",
            )

        wrapped = handler
        for mw in self._middlewares + tuple(middleware):
            wrapped = mw(wrapped)

        host_segments, path_segments = _split_pattern(pattern)
        new_tree = Node(
            kind="method",
            route_handler=wrapped,
            not_found_handler=self._not_found_handler,
        )
        new_tree = Node(
            kind="path",
            not_found_handler=self._not_found_handler,
            exact={method: new_tree},
        )
        new_tree = _wrap_path_segments(new_tree, path_segments, self._not_found_handler)
        if host_segments:
            new_tree = _wrap_host_segments(
                new_tree, host_segments, self._not_found_handler
            )

        self.root = _merge_trees(self.root, new_tree)
        self._find_handler.cache_clear()

        if name is not None:
            route_pattern = (
                f"{'.'.join(host_segments)}/{'/'.join(path_segments)}"
                if host_segments
                else f"/{'/'.join(path_segments)}"
            )
            self.named_routes = MappingProxyType(
                dict(self.named_routes) | {name: route_pattern.replace("...}", "}")}
            )

    def get(
        self,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Shorthand for ``handle("GET", ...)``."""
        self.handle("GET", pattern, handler, middleware=middleware, name=name)

    def post(
        self,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Shorthand for ``handle("POST", ...)``."""
        self.handle("POST", pattern, handler, middleware=middleware, name=name)

    def put(
        self,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Shorthand for ``handle("PUT", ...)``."""
        self.handle("PUT", pattern, handler, middleware=middleware, name=name)

    def delete(
        self,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Shorthand for ``handle("DELETE", ...)``."""
        self.handle("DELETE", pattern, handler, middleware=middleware, name=name)

    def patch(
        self,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Shorthand for ``handle("PATCH", ...)``."""
        self.handle("PATCH", pattern, handler, middleware=middleware, name=name)

    def head(
        self,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Shorthand for ``handle("HEAD", ...)``."""
        self.handle("HEAD", pattern, handler, middleware=middleware, name=name)

    def options(
        self,
        pattern: str,
        handler: Handler,
        *,
        middleware: Sequence[Middleware] = (),
        name: str | None = None,
    ) -> None:
        """Shorthand for ``handle("OPTIONS", ...)``."""
        self.handle("OPTIONS", pattern, handler, middleware=middleware, name=name)

    def mount(
        self,
        pattern: str,
        child: Subtree,
        *,
        middleware: Sequence[Middleware] = (),
    ) -> None:
        """Graft another router or static tree under ``pattern`` without copying its handlers manually.

        Parameters:
            pattern: Mount point (same syntax as routes; must not end with a catch-all segment).
            child: Object exposing ``root`` (trie node) and ``named_routes`` (e.g. another ``Router`` or ``StaticAssets``).
            middleware: Extra middleware applied only to routes coming from ``child``.

        Raises:
            StarioError: If ``child`` is not a subtree, host rules conflict, or named routes collide.

        Notes:
            Static assets and sub-routers both implement the ``Subtree`` protocol expected here.
        """
        if not hasattr(child, "root") or not hasattr(child, "named_routes"):
            raise StarioError(
                "Can only mount subtrees",
                context={"child": child},
                help_text="Pass an object with `root` and `named_routes` attributes to mount().",
            )

        host_segments, path_segments = _split_pattern(pattern)
        if (
            path_segments
            and path_segments[-1].startswith("{")
            and path_segments[-1].endswith("...}")
        ):
            raise StarioError(
                "Catchall mount prefix cannot have child routes",
                context={"pattern": pattern},
                help_text="Mount the catchall route directly with handle()/get()/head() instead of mounting a subtree beneath it.",
            )
        if host_segments and child.root.kind == "host":
            raise StarioError(
                "Host matching already defined",
                context={"pattern": pattern, "child": child},
                help_text="Define host matching either on the mounted subtree or in mount(), not both.",
            )

        for name, child_pattern in child.named_routes.items():
            if name in self.named_routes:
                raise StarioError(
                    "Name already registered",
                    context={"name": name, "pattern": child_pattern},
                    help_text="Use a different name to avoid conflicts.",
                )

        mounted_root = child.root
        middlewares = self._middlewares + tuple(middleware)
        if middlewares:
            mounted_root = _apply_middlewares(mounted_root, middlewares)
        if host_segments:
            mounted_root = _wrap_host_segments(
                mounted_root,
                host_segments,
                self._not_found_handler,
            )

        mounted_root = _prefix_path_tree(
            mounted_root,
            [] if path_segments == [""] else path_segments,
            self._not_found_handler,
        )
        self.root = _merge_trees(self.root, mounted_root)
        self._find_handler.cache_clear()

        prefixed_named_routes = {
            name: _prefix_named_pattern(
                child_pattern,
                mount_pattern=pattern,
                host_segments=host_segments,
                path_segments=path_segments,
            )
            for name, child_pattern in child.named_routes.items()
        }
        if prefixed_named_routes:
            self.named_routes = MappingProxyType(
                dict(self.named_routes) | prefixed_named_routes
            )
