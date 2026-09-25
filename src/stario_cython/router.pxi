"""Compiled text trie for route lookup. No result cache — walk is the hit path."""

from cpython.unicode cimport PyUnicode_AsUTF8AndSize

cdef object _ROUTER_EMPTY_ROUTE = None
cdef object _ROUTER_EMPTY_MATCH = None
cdef object _ROUTER_DEFAULT_NF = None
cdef object _ROUTER_DEFAULT_MNA = None
cdef object _ROUTER_MATCH_CLS = None


cdef void _router_symbols():
    global _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH
    global _ROUTER_DEFAULT_NF, _ROUTER_DEFAULT_MNA, _ROUTER_MATCH_CLS
    if _ROUTER_EMPTY_ROUTE is not None:
        return
    from stario.http.context import EMPTY_MATCH, Match
    from stario.http.dispatch import default_not_found, method_not_allowed_handler
    from stario.http.route import EMPTY_ROUTE
    _ROUTER_EMPTY_ROUTE = EMPTY_ROUTE
    _ROUTER_EMPTY_MATCH = EMPTY_MATCH
    _ROUTER_DEFAULT_NF = default_not_found
    _ROUTER_DEFAULT_MNA = method_not_allowed_handler
    _ROUTER_MATCH_CLS = Match


cdef CNode _compile_node(object node):
    cdef CNode out = CNode.__new__(CNode)
    cdef CEdge edge
    cdef list edges = []
    cdef object key
    cdef object child
    cdef object extra
    cdef object rest_map
    cdef object endpoints
    cdef object method
    cdef object endpoint
    cdef bytes key_b
    cdef bytes rest_b
    cdef Py_ssize_t i
    cdef int first
    cdef int last_first
    cdef int run_start
    out.wildcard_name = None
    out.wildcard = None
    out.catchall_name = None
    out.catchall = None
    out.endpoints = None
    out.method_set = None
    out.not_found = node.not_found_handler
    out.not_found_custom = node.not_found_handler is not None
    out.method_na = node.method_not_allowed_handler
    for i in range(256):
        out.edge_start[i] = -1
        out.edge_count[i] = 0
    rest_map = node.rest
    for key, child in node.exact.items():
        edge = CEdge.__new__(CEdge)
        key_b = key.encode("utf-8") if not isinstance(key, bytes) else key
        edge.key = key_b
        edge.key_n = PyBytes_GET_SIZE(key_b)
        edge.key_p = PyBytes_AS_STRING(key_b)
        extra = rest_map.get(key, "") if rest_map is not None else ""
        if extra:
            rest_b = extra.encode("utf-8") if not isinstance(extra, bytes) else extra
            edge.rest = rest_b
            edge.rest_n = PyBytes_GET_SIZE(rest_b)
            edge.rest_p = PyBytes_AS_STRING(rest_b)
        else:
            edge.rest = None
            edge.rest_n = 0
            edge.rest_p = NULL
        edge.child = _compile_node(child)
        edges.append(edge)
    edges.sort(key=lambda item: (
        0 if (<CEdge>item).key_n == 0 else 1,
        (<CEdge>item).key,
    ))
    out.edges = edges
    out.n_edges = <Py_ssize_t>len(edges)
    last_first = -1
    run_start = 0
    for i in range(out.n_edges):
        edge = <CEdge>edges[i]
        if edge.key_n == 0:
            first = 0
        else:
            first = <int><unsigned char>edge.key_p[0]
        if last_first < 0:
            last_first = first
            run_start = <int>i
            out.edge_start[first] = <int>i
        elif first != last_first:
            out.edge_count[last_first] = <int>i - run_start
            last_first = first
            run_start = <int>i
            out.edge_start[first] = <int>i
    if last_first >= 0:
        out.edge_count[last_first] = <int>out.n_edges - run_start
    if node.wildcard is not None:
        out.wildcard_name = node.wildcard_name
        out.wildcard = _compile_node(node.wildcard)
    if node.catchall is not None:
        out.catchall_name = node.catchall_name
        out.catchall = _compile_node(node.catchall)
    endpoints = node.endpoints
    if endpoints:
        out.endpoints = {}
        for method, endpoint in endpoints.items():
            out.endpoints[method] = (endpoint.handler, endpoint.route)
        out.method_set = frozenset(endpoints)
    return out


cdef inline void _set_param(dict params, object name, object value):
    params[name] = value


cdef CNode _take_param(
    CNode node,
    const char* seg,
    Py_ssize_t seglen,
    const char* rest,
    Py_ssize_t restlen,
    dict params,
):
    cdef object decoded
    if node.wildcard is not None and node.wildcard_name is not None:
        decoded = PyUnicode_DecodeUTF8(seg, seglen, "surrogatepass")
        _set_param(params, node.wildcard_name, decoded)
        return node.wildcard
    if node.catchall is not None:
        if node.catchall_name is not None:
            if rest != NULL and restlen >= 0:
                decoded = PyUnicode_DecodeUTF8(rest, restlen, "surrogatepass")
            else:
                decoded = PyUnicode_DecodeUTF8(seg, seglen, "surrogatepass")
            _set_param(params, node.catchall_name, decoded)
        return node.catchall
    return None


cdef CNode _match_exact(
    CNode node,
    const char* path,
    Py_ssize_t n,
    Py_ssize_t i,
    Py_ssize_t end,
    Py_ssize_t* nxt,
):
    cdef CEdge edge
    cdef Py_ssize_t seglen = end - i
    cdef Py_ssize_t after
    cdef Py_ssize_t bound
    cdef int first
    cdef int start
    cdef int count
    cdef int j
    if seglen == 0:
        first = 0
    else:
        first = <int><unsigned char>path[i]
    start = node.edge_start[first]
    if start < 0:
        return None
    count = node.edge_count[first]
    for j in range(count):
        edge = <CEdge>node.edges[start + j]
        if edge.key_n != seglen:
            continue
        if seglen and memcmp(edge.key_p, path + i, <size_t>seglen) != 0:
            continue
        if edge.rest_n == 0:
            nxt[0] = end + 1
            return edge.child
        after = end + 1
        bound = after + edge.rest_n
        if (
            end >= n
            or path[end] != 47
            or bound > n
            or memcmp(path + after, edge.rest_p, <size_t>edge.rest_n) != 0
            or (bound < n and path[bound] != 47)
        ):
            continue
        nxt[0] = bound + 1 if bound < n else bound
        return edge.child
    return None


cdef class _WalkCur:
    cdef CNode node
    cdef object not_found
    cdef object method_na
    cdef bint custom
    cdef dict params


cdef inline void _enter_node(_WalkCur cur, CNode child):
    if child.not_found is not None:
        cur.not_found = child.not_found
        cur.custom = True
    if child.method_na is not None:
        cur.method_na = child.method_na
    cur.node = child


cdef bint _walk_path(_WalkCur cur, const char* path, Py_ssize_t n):
    cdef Py_ssize_t i
    cdef Py_ssize_t end
    cdef Py_ssize_t nxt
    cdef Py_ssize_t slash
    cdef CNode node
    cdef CNode child
    if n == 1 and path[0] == 47:
        return True
    i = 1
    while i <= n:
        node = cur.node
        if i == n:
            if path[n - 1] != 47:
                break
            end = n
            nxt = n + 1
        else:
            slash = i
            while slash < n and path[slash] != 47:
                slash += 1
            end = slash
            nxt = end + 1
        child = _match_exact(node, path, n, i, end, &nxt)
        if child is None:
            if node.catchall is not None:
                child = _take_param(
                    node,
                    path + i,
                    end - i,
                    path + i,
                    n - i,
                    cur.params,
                )
            else:
                child = _take_param(
                    node,
                    path + i,
                    end - i,
                    NULL,
                    -1,
                    cur.params,
                )
            if child is None:
                return False
            if child is node.catchall:
                nxt = n + 1
        _enter_node(cur, child)
        i = nxt
    return True


cdef bint _walk_host(_WalkCur cur, const char* host, Py_ssize_t n):
    cdef Py_ssize_t end = n
    cdef Py_ssize_t dot
    cdef Py_ssize_t seg_start
    cdef CNode node
    cdef CNode child
    cdef Py_ssize_t unused = 0
    if n == 0:
        return True
    while end > 0:
        node = cur.node
        dot = end - 1
        while dot >= 0 and host[dot] != 46:
            dot -= 1
        seg_start = dot + 1
        child = _match_exact(node, host, n, seg_start, end, &unused)
        if child is None:
            if node.catchall is not None:
                child = _take_param(
                    node,
                    host + seg_start,
                    end - seg_start,
                    host,
                    end,
                    cur.params,
                )
            else:
                child = _take_param(
                    node,
                    host + seg_start,
                    end - seg_start,
                    NULL,
                    -1,
                    cur.params,
                )
            if child is None:
                return False
            if child is node.catchall:
                end = 0
            elif dot >= 0:
                end = dot
            else:
                end = 0
        else:
            end = dot if dot >= 0 else 0
        _enter_node(cur, child)
    return True


cdef object _resolve_tagged(CNode root, object path, object method, object host):
    """Return ``(route_match, status, custom)``.

    status: 0 found, 1 method_not_allowed, 2 not_found
    """
    cdef _WalkCur cur = _WalkCur.__new__(_WalkCur)
    cdef const char* path_p
    cdef const char* host_p = NULL
    cdef Py_ssize_t path_n
    cdef Py_ssize_t host_n = 0
    cdef object nf_before
    cdef bint custom_before
    cdef object endpoints
    cdef object hit
    cdef object handler
    cdef object route
    cur.node = root
    cur.not_found = root.not_found if root.not_found is not None else _ROUTER_DEFAULT_NF
    cur.custom = root.not_found_custom
    cur.method_na = root.method_na
    cur.params = {}
    path_p = PyUnicode_AsUTF8AndSize(path, &path_n)
    if host:
        host_p = PyUnicode_AsUTF8AndSize(host, &host_n)
        nf_before = cur.not_found
        custom_before = cur.custom
        if not _walk_host(cur, host_p, host_n):
            return (
                (nf_before, _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH),
                2,
                custom_before,
            )
    nf_before = cur.not_found
    custom_before = cur.custom
    if not _walk_path(cur, path_p, path_n):
        return (
            (nf_before, _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH),
            2,
            custom_before,
        )
    endpoints = cur.node.endpoints
    if endpoints is None:
        return (
            (cur.not_found, _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH),
            2,
            cur.custom,
        )
    hit = endpoints.get(method)
    if hit is None:
        if endpoints:
            handler = (cur.method_na or _ROUTER_DEFAULT_MNA)(cur.node.method_set)
            return (
                (handler, _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH),
                1,
                cur.custom,
            )
        return (
            (cur.not_found, _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH),
            2,
            cur.custom,
        )
    handler = hit[0]
    route = hit[1]
    if cur.params:
        return (
            (handler, route, _ROUTER_MATCH_CLS(route.pattern, cur.params)),
            0,
            cur.custom,
        )
    return (
        (handler, route, _ROUTER_MATCH_CLS(route.pattern)),
        0,
        cur.custom,
    )


cdef inline object _exact_hit(dict table, object path, object method):
    """path -> method -> hit. No (host, path, method) tuple on the lookup."""
    cdef object methods
    if table is None:
        return None
    methods = table.get(path)
    if methods is None:
        return None
    return methods.get(method)


cdef object _router_lookup(CRouter self, object host, object path, object method):
    cdef object hit
    cdef object host_pack
    cdef object path_pack
    cdef object hroot
    cdef object host_map
    cdef int host_status
    cdef int path_status
    cdef bint host_custom
    if host is None:
        host = ""
    if host:
        host_map = self.exact_hosts.get(host)
        if host_map is not None:
            hit = _exact_hit(<dict>host_map, path, method)
            if hit is not None:
                return hit
        if self.host_routing:
            hroot = self.hosts_exact.get(host)
            if hroot is not None:
                host_pack = _resolve_tagged(<CNode>hroot, path, method, "")
            elif self.has_param_hosts:
                host_pack = _resolve_tagged(self.hosts_param, path, method, host)
            else:
                host_pack = (
                    (_ROUTER_DEFAULT_NF, _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH),
                    2,
                    False,
                )
            host_status = <int>host_pack[1]
            host_custom = <bint>host_pack[2]
            if host_status == 0:
                return host_pack[0]
            hit = _exact_hit(self.exact_paths, path, method)
            if hit is not None:
                return hit
            path_pack = _resolve_tagged(self.path, path, method, "")
            path_status = <int>path_pack[1]
            if path_status == 0:
                return path_pack[0]
            if host_status == 1:
                return host_pack[0]
            if path_status == 1:
                return path_pack[0]
            if host_custom:
                return host_pack[0]
            return path_pack[0]
    hit = _exact_hit(self.exact_paths, path, method)
    if hit is not None:
        return hit
    return _resolve_tagged(self.path, path, method, "")[0]


@cython.final
cdef class CEdge:
    pass


@cython.final
cdef class CNode:
    pass


@cython.final
cdef class CRouter:
    def lookup(self, host, path, method):
        return self.c_lookup(host, path, method)

    cdef object c_lookup(self, object host, object path, object method):
        _router_symbols()
        return _router_lookup(self, host, path, method)


@cython.final
cdef class AppState:
    def __cinit__(self):
        self.host_routing = False
        self.shutting_down = False
        self.router = None


cdef void _index_exact(dict dest, object path, object method, object hit):
    cdef object methods = dest.get(path)
    if methods is None:
        methods = {}
        dest[path] = methods
    (<dict>methods)[method] = hit


cpdef CRouter compile_router(object router):
    cdef CRouter compiled = CRouter.__new__(CRouter)
    cdef dict hosts
    cdef dict exact_paths
    cdef dict exact_hosts
    cdef dict host_map
    cdef object host
    cdef object tree
    cdef object key
    cdef object hit
    _router_symbols()
    exact_paths = {}
    exact_hosts = {}
    for key, hit in router._exact.items():
        host = key[0]
        if host:
            host_map = exact_hosts.get(host)
            if host_map is None:
                host_map = {}
                exact_hosts[host] = host_map
            _index_exact(<dict>host_map, key[1], key[2], hit)
        else:
            _index_exact(exact_paths, key[1], key[2], hit)
    compiled.exact_paths = exact_paths
    compiled.exact_hosts = exact_hosts
    compiled.host_routing = router._host_routing
    compiled.has_param_hosts = router._has_param_hosts
    compiled.path = _compile_node(router._path)
    compiled.hosts_param = _compile_node(router._hosts_param)
    hosts = {}
    for host, tree in router._hosts_exact.items():
        hosts[host] = _compile_node(tree)
    compiled.hosts_exact = hosts
    return compiled
