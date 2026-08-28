# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""Cython route table: static exact map, in-place host/path walks, nested cache.

Registration stays compatible with ``stario.http.dispatch.Router``. The request
hot path avoids ``UrlPath.request_path`` / ``request_host`` tuple splits and
``functools.lru_cache`` wrapper overhead:

- hostless exact routes (``/plaintext``, ``/json``, …) hit ``_static[path][method]``
- parameterized routes use a nested ``host -> path -> method`` dict (cap 1024)
- cache misses walk the unicode path/host in place
"""

from functools import lru_cache
from types import MappingProxyType

from cpython.dict cimport PyDict_GetItem, PyDict_SetItem
from cpython.object cimport PyObject
from cpython.unicode cimport PyUnicode_GET_LENGTH, PyUnicode_READ_CHAR

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.context import (
    EMPTY_ROUTE_MATCH,
    Context,
    RouteMatch,
)
from stario.http.writer import Writer
from stario.routing import Route, UrlPath

cdef int CACHE_MAX = 1024
cdef int ST_FOUND = 0
cdef int ST_MNA = 1
cdef int ST_NF = 2

cdef object KIND_CATCHALL = "catchall"
cdef object KIND_WILDCARD = "wildcard"
cdef object KIND_EXACT = "exact"
cdef object EMPTY_METHODS = frozenset()
cdef object EMPTY_HOST = ""
cdef object SLASH = "/"


cdef class Node:
    def __cinit__(self):
        self.not_found_handler = None
        self.method_not_allowed_handler = None
        self.middleware = ()
        self.host_depth = 0
        self.exact = {}
        self.wildcard_name = None
        self.wildcard = None
        self.catchall_name = None
        self.catchall = None
        self.endpoints = None
        self.methods = EMPTY_METHODS

    def __init__(self, host_depth=0):
        self.host_depth = host_depth


cdef class Endpoint:
    def __init__(self, handler, route_match):
        self.handler = handler
        self.route_match = route_match


cdef inline object _as_pattern(object path):
    return path if isinstance(path, UrlPath) else UrlPath(path)


cdef inline bint _kind_is(object kind, object interned):
    return kind is interned or kind == interned


cdef Node _pattern_child(Node current, object segment):
    cdef object kind = segment.kind
    cdef object child
    cdef PyObject *p
    if _kind_is(kind, KIND_CATCHALL):
        if current.catchall is None or current.catchall_name != segment.name:
            return None
        return current.catchall
    if _kind_is(kind, KIND_WILDCARD):
        if current.wildcard is None or current.wildcard_name != segment.name:
            return None
        return current.wildcard
    p = PyDict_GetItem(current.exact, segment.name)
    if p == NULL:
        return None
    return <Node>p


cdef Node trie_descend(Node root, object pattern, bint create, bint host_tree):
    cdef Node current = root
    cdef Node child
    cdef object segment
    for segment in pattern.host_trie():
        if create:
            current = _descend_or_create(current, segment, host_tree)
        else:
            child = _pattern_child(current, segment)
            if child is None:
                return current
            current = child
    for segment in pattern.path:
        if create:
            current = _descend_or_create(current, segment, False)
        else:
            child = _pattern_child(current, segment)
            if child is None:
                return current
            current = child
    return current


cdef list _collect_middleware(Node tree, object pattern, bint path_only):
    cdef list middlewares = list(tree.middleware)
    cdef Node current = tree
    cdef Node child
    cdef object segment
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


cdef bint _branch_has_endpoints(Node node):
    cdef object child
    if node.endpoints:
        return True
    if node.wildcard is not None and _branch_has_endpoints(node.wildcard):
        return True
    if node.catchall is not None and _branch_has_endpoints(node.catchall):
        return True
    for child in node.exact.values():
        if _branch_has_endpoints(<Node>child):
            return True
    return False


cdef bint _pattern_is_static(object pattern):
    cdef object segment
    if pattern.host:
        return False
    for segment in pattern.path:
        if not _kind_is(segment.kind, KIND_EXACT):
            return False
    return True


async def default_not_found(_c: Context, w: Writer):
    responses.text(w, "Not Found", 404)


@lru_cache(maxsize=256)
def method_not_allowed_handler(allowed):
    allow_header = ", ".join(sorted(allowed))

    async def respond(_c, w):
        w.headers.set("Allow", allow_header)
        responses.text(w, "Method Not Allowed", 405)

    return respond


cdef object _walk_path(
    Node current,
    object path,
    object params,
    object not_found,
    bint not_found_custom,
    object method_na,
):
    cdef Py_ssize_t n = PyUnicode_GET_LENGTH(path)
    cdef Py_ssize_t start
    cdef Py_ssize_t slash
    cdef Py_ssize_t path_off
    cdef object seg
    cdef object name
    cdef object nf
    cdef object mna
    cdef PyObject *p
    cdef Node node = current
    cdef Node nxt

    if n == 1 and PyUnicode_READ_CHAR(path, 0) == 47:
        return (node, params, not_found, not_found_custom, method_na)

    start = 1
    path_off = 1
    while start < n:
        slash = path.find(SLASH, start)
        if slash == -1:
            seg = path[start:]
        else:
            seg = path[start:slash]
        p = PyDict_GetItem(node.exact, seg)
        if p != NULL:
            nxt = <Node>p
            if slash == -1:
                start = n
            else:
                start = slash + 1
            path_off = start
        elif node.wildcard is not None:
            if params is None:
                params = {}
            params[node.wildcard_name] = seg
            nxt = node.wildcard
            if slash == -1:
                start = n
            else:
                start = slash + 1
            path_off = start
        elif node.catchall is not None:
            name = node.catchall_name
            if name is not None:
                if params is None:
                    params = {}
                params[name] = path[path_off:]
            nxt = node.catchall
            start = n
        else:
            return None
        nf = nxt.not_found_handler
        if nf is not None:
            not_found = nf
            not_found_custom = True
        mna = nxt.method_not_allowed_handler
        if mna is not None:
            method_na = mna
        node = nxt
    return (node, params, not_found, not_found_custom, method_na)


cdef object _walk_host(
    Node current,
    object host,
    object params,
    object not_found,
    bint not_found_custom,
    object method_na,
):
    cdef Py_ssize_t n = PyUnicode_GET_LENGTH(host)
    cdef Py_ssize_t end
    cdef Py_ssize_t dot
    cdef Py_ssize_t rest_end
    cdef object seg
    cdef object name
    cdef object nf
    cdef object mna
    cdef PyObject *p
    cdef Node node = current
    cdef Node nxt

    if n == 0:
        return (node, params, not_found, not_found_custom, method_na)

    end = n
    while end > 0:
        rest_end = end
        dot = host.rfind(".", 0, end)
        if dot == -1:
            seg = host[:end]
        else:
            seg = host[dot + 1:end]
        p = PyDict_GetItem(node.exact, seg)
        if p != NULL:
            nxt = <Node>p
            end = 0 if dot == -1 else dot
        elif node.wildcard is not None:
            if params is None:
                params = {}
            params[node.wildcard_name] = seg
            nxt = node.wildcard
            end = 0 if dot == -1 else dot
        elif node.catchall is not None:
            name = node.catchall_name
            if name is not None:
                if params is None:
                    params = {}
                params[name] = host[:rest_end]
            nxt = node.catchall
            end = 0
        else:
            return None
        nf = nxt.not_found_handler
        if nf is not None:
            not_found = nf
            not_found_custom = True
        mna = nxt.method_not_allowed_handler
        if mna is not None:
            method_na = mna
        node = nxt
    return (node, params, not_found, not_found_custom, method_na)


cdef tuple _finish_resolve(
    Node current,
    object params,
    object not_found,
    bint not_found_custom,
    object method_na,
    object method,
):
    cdef Endpoint endpoint
    cdef PyObject *p
    cdef dict endpoints = current.endpoints

    if endpoints is not None:
        p = PyDict_GetItem(endpoints, method)
        if p != NULL:
            endpoint = <Endpoint>p
            if params is None:
                return (
                    endpoint.handler,
                    endpoint.route_match,
                    ST_FOUND,
                    not_found_custom,
                )
            return (
                endpoint.handler,
                RouteMatch(
                    pattern=endpoint.route_match.pattern,
                    params=MappingProxyType(params),
                ),
                ST_FOUND,
                not_found_custom,
            )

    if current.methods:
        return (
            (method_na or method_not_allowed_handler)(current.methods),
            EMPTY_ROUTE_MATCH,
            ST_MNA,
            not_found_custom,
        )
    return not_found, EMPTY_ROUTE_MATCH, ST_NF, not_found_custom


cdef tuple _resolve(Node root, object host, object path, object method):
    cdef object params = None
    cdef object not_found = root.not_found_handler
    cdef bint not_found_custom = not_found is not None
    cdef object method_na = root.method_not_allowed_handler
    cdef object walked
    cdef Node current = root

    if not_found is None:
        not_found = default_not_found

    if host:
        walked = _walk_host(
            current, host, params, not_found, not_found_custom, method_na
        )
        if walked is None:
            return not_found, EMPTY_ROUTE_MATCH, ST_NF, not_found_custom
        current = <Node>walked[0]
        params = walked[1]
        not_found = walked[2]
        not_found_custom = walked[3]
        method_na = walked[4]

    walked = _walk_path(
        current, path, params, not_found, not_found_custom, method_na
    )
    if walked is None:
        return not_found, EMPTY_ROUTE_MATCH, ST_NF, not_found_custom
    current = <Node>walked[0]
    params = walked[1]
    not_found = walked[2]
    not_found_custom = walked[3]
    method_na = walked[4]
    return _finish_resolve(
        current, params, not_found, not_found_custom, method_na, method
    )


cdef Node _descend_or_create(Node current, object segment, bint host_label):
    cdef Node child = _pattern_child(current, segment)
    cdef object kind
    cdef object name
    cdef int child_host_depth
    if child is not None:
        return child

    kind = segment.kind
    name = segment.name
    child_host_depth = current.host_depth + (1 if host_label else 0)

    if _kind_is(kind, KIND_CATCHALL):
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

    if _kind_is(kind, KIND_WILDCARD):
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
    current.exact[name] = child
    return child


cdef class Router:
    """Route table: host routes override hostless defaults when they fully match."""

    def __cinit__(self):
        self._path = Node()
        self._hosts = Node()
        self._has_hosts = False
        self._static = {}
        self._cache = {}
        self._cache_n = 0

    @property
    def host_routing(self):
        return self._has_hosts

    cdef void _clear_match_cache(self):
        self._cache = {}
        self._cache_n = 0

    cdef void _remember_static(self, object pattern, object method, object result):
        cdef dict by_method
        cdef PyObject *p
        if not _pattern_is_static(pattern):
            return
        p = PyDict_GetItem(self._static, pattern.text)
        if p == NULL:
            by_method = {}
            PyDict_SetItem(self._static, pattern.text, by_method)
        else:
            by_method = <dict>p
        PyDict_SetItem(by_method, method, result)

    cpdef tuple find_handler(self, object host, object path, object method):
        cdef dict by_host
        cdef dict by_path
        cdef dict by_method
        cdef tuple result
        cdef PyObject *p

        # Hostless exact routes: one/two dict lookups, no cache tuple hashing.
        # Skip when host routing is active AND the request has a Host — a host
        # tree hit must win over a hostless exact route of the same path.
        if not self._has_hosts or not host:
            p = PyDict_GetItem(self._static, path)
            if p != NULL:
                p = PyDict_GetItem(<dict>p, method)
                if p != NULL:
                    return <tuple>p

        by_host = self._cache
        p = PyDict_GetItem(by_host, host)
        by_path = <dict>p if p != NULL else None
        if by_path is not None:
            p = PyDict_GetItem(by_path, path)
            by_method = <dict>p if p != NULL else None
            if by_method is not None:
                p = PyDict_GetItem(by_method, method)
                if p != NULL:
                    return <tuple>p
        else:
            by_method = None

        result = self._match(host, path, method)
        if self._cache_n >= CACHE_MAX:
            self._cache = {}
            self._cache_n = 0
            by_host = self._cache
            by_path = None
            by_method = None
        if by_path is None:
            by_path = {}
            PyDict_SetItem(by_host, host, by_path)
            by_method = None
        if by_method is None:
            by_method = {}
            PyDict_SetItem(by_path, path, by_method)
        PyDict_SetItem(by_method, method, result)
        self._cache_n += 1
        return result

    cdef tuple _match(self, object host, object path, object method):
        cdef object host_key
        cdef object host_handler
        cdef object host_match
        cdef int host_status
        cdef bint host_nf_custom
        cdef object path_handler
        cdef object path_match
        cdef int path_status
        cdef tuple resolved

        if self._has_hosts and host:
            host_key = host.lower() if type(host) is str else host
            resolved = _resolve(self._hosts, host_key, path, method)
            host_handler = resolved[0]
            host_match = resolved[1]
            host_status = resolved[2]
            host_nf_custom = resolved[3]
            if host_status == ST_FOUND:
                return host_handler, host_match
            resolved = _resolve(self._path, EMPTY_HOST, path, method)
            path_handler = resolved[0]
            path_match = resolved[1]
            path_status = resolved[2]
            if path_status == ST_FOUND:
                return path_handler, path_match
            if host_status == ST_MNA:
                return host_handler, host_match
            if path_status == ST_MNA:
                return path_handler, path_match
            if host_nf_custom:
                return host_handler, host_match
            return path_handler, path_match

        resolved = _resolve(self._path, EMPTY_HOST, path, method)
        return resolved[0], resolved[1]

    cdef Node _registration_tree(self, object pattern):
        if pattern.host:
            self._has_hosts = True
            return self._hosts
        return self._path

    cdef Node _leaf_node(self, object pattern):
        cdef Node tree = self._registration_tree(pattern)
        return trie_descend(tree, pattern, True, tree is self._hosts)

    cdef Node _policy_node(self, object pattern):
        cdef object route = _as_pattern(pattern)
        cdef object segment
        if any(segment.kind == KIND_CATCHALL for segment in route.host):
            raise StarioError(
                "Catchall host policy is not supported",
                context={"pattern": route.text},
                help_text=(
                    "Use an exact host prefix such as "
                    "UrlPath('/', host='api.example.com') or apply path policy like '/api'."
                ),
            )
        cdef Py_ssize_t npath = len(route.path)
        if npath and route.path[npath - 1].kind == KIND_CATCHALL:
            raise StarioError(
                "Catchall route policy cannot have child routes",
                context={"pattern": route.text},
                help_text="Use a non-catchall prefix such as '/api' for route policy.",
            )
        return self._leaf_node(route)

    def use(self, pattern, *middleware):
        cdef Node current = self._policy_node(pattern)
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

    def not_found(self, pattern, handler):
        self._policy_node(pattern).not_found_handler = handler
        self._clear_match_cache()

    def method_not_allowed(self, pattern, handler):
        self._policy_node(pattern).method_not_allowed_handler = handler
        self._clear_match_cache()

    def handle(self, method, path, handler, *, middleware=()):
        cdef object route = _as_pattern(path)
        cdef Node tree
        cdef Node current
        cdef list scoped_middleware
        cdef object wrapped
        cdef object mw
        cdef object existing
        cdef Endpoint endpoint
        cdef dict endpoints
        cdef tuple result
        method = method.upper()
        tree = self._registration_tree(route)
        scoped_middleware = _collect_middleware(tree, route, False)
        if route.host:
            scoped_middleware.extend(_collect_middleware(self._path, route, True))

        current = trie_descend(tree, route, True, tree is self._hosts)

        wrapped = handler
        for mw in reversed([*scoped_middleware, *middleware]):
            wrapped = mw(wrapped)

        endpoints = current.endpoints
        existing = None if endpoints is None else endpoints.get(method)
        if existing is not None:
            raise StarioError(
                "Route already registered",
                context={"method": method, "pattern": route.text},
                help_text="Each HTTP method may be registered only once per route pattern.",
            )

        endpoint = Endpoint(
            wrapped,
            RouteMatch(
                pattern=route.text,
                params=EMPTY_ROUTE_MATCH.params,
            ),
        )
        if endpoints is None:
            current.endpoints = {method: endpoint}
        else:
            endpoints[method] = endpoint
        if method not in current.methods:
            current.methods = current.methods | frozenset({method})
        result = (wrapped, endpoint.route_match)
        self._remember_static(route, method, result)
        self._clear_match_cache()

    def add(self, route, handler, *, middleware=()):
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

    def get(self, path, handler, *, middleware=()):
        self.handle("GET", path, handler, middleware=middleware)

    def query(self, path, handler, *, middleware=()):
        self.handle("QUERY", path, handler, middleware=middleware)

    def post(self, path, handler, *, middleware=()):
        self.handle("POST", path, handler, middleware=middleware)

    def put(self, path, handler, *, middleware=()):
        self.handle("PUT", path, handler, middleware=middleware)

    def delete(self, path, handler, *, middleware=()):
        self.handle("DELETE", path, handler, middleware=middleware)

    def patch(self, path, handler, *, middleware=()):
        self.handle("PATCH", path, handler, middleware=middleware)

    def head(self, path, handler, *, middleware=()):
        self.handle("HEAD", path, handler, middleware=middleware)

    def options(self, path, handler, *, middleware=()):
        self.handle("OPTIONS", path, handler, middleware=middleware)


__all__ = [
    "Router",
    "default_not_found",
    "method_not_allowed_handler",
]
