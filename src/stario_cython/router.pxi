"""Compiled path/host trie. Walk UTF-8 bytes; intern results on the leaves.

No exact-map sidecar and no per-lookup walk object. Static hits return the
3-tuple stored at compile time (same Match identity). Param hits allocate
one Match + one params dict. 404/405 tuples are interned on the node.
"""

from cpython.unicode cimport PyUnicode_AsUTF8AndSize, PyUnicode_DecodeUTF8

cdef object _ROUTER_EMPTY_ROUTE = None
cdef object _ROUTER_EMPTY_MATCH = None
cdef object _ROUTER_DEFAULT_NF = None
cdef object _ROUTER_DEFAULT_MNA = None
cdef object _ROUTER_MATCH_CLS = None
cdef object _NF_HIT = None


cdef void _router_symbols():
    global _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH
    global _ROUTER_DEFAULT_NF, _ROUTER_DEFAULT_MNA, _ROUTER_MATCH_CLS, _NF_HIT
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
    _NF_HIT = (default_not_found, EMPTY_ROUTE, EMPTY_MATCH)


cdef object _pack(object handler, object route, object match):
    return (handler, route, match)


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
    cdef object hit
    cdef object match
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
    out.one_method = None
    out.one_hit = None
    out.one_edge = None
    out.nf_hit = None
    out.mna_hit = None
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
    if out.n_edges == 1:
        out.one_edge = <CEdge>edges[0]
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
    if out.not_found is not None:
        out.nf_hit = _pack(out.not_found, _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH)
    endpoints = node.endpoints
    if endpoints:
        out.endpoints = {}
        for method, endpoint in endpoints.items():
            match = _ROUTER_MATCH_CLS(endpoint.route.pattern)
            hit = _pack(endpoint.handler, endpoint.route, match)
            out.endpoints[method] = hit
            if out.one_method is None:
                out.one_method = method
                out.one_hit = hit
            else:
                out.one_method = None
                out.one_hit = None
        out.method_set = frozenset(endpoints)
        out.mna_hit = _pack(
            (out.method_na or _ROUTER_DEFAULT_MNA)(out.method_set),
            _ROUTER_EMPTY_ROUTE,
            _ROUTER_EMPTY_MATCH,
        )
    return out


cdef inline CNode _match_edge(
    CEdge edge,
    const char* path,
    Py_ssize_t n,
    Py_ssize_t i,
    Py_ssize_t end,
    Py_ssize_t* nxt,
):
    cdef Py_ssize_t seglen = end - i
    cdef Py_ssize_t after
    cdef Py_ssize_t bound
    if edge.key_n != seglen:
        return None
    if seglen and memcmp(edge.key_p, path + i, <size_t>seglen) != 0:
        return None
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
        return None
    nxt[0] = bound + 1 if bound < n else bound
    return edge.child


cdef CNode _match_exact(
    CNode node,
    const char* path,
    Py_ssize_t n,
    Py_ssize_t i,
    Py_ssize_t end,
    Py_ssize_t* nxt,
):
    cdef CEdge edge
    cdef int first
    cdef int start
    cdef int count
    cdef int j
    cdef CNode child
    if node.n_edges == 1:
        return _match_edge(node.one_edge, path, n, i, end, nxt)
    if end == i:
        first = 0
    else:
        first = <int><unsigned char>path[i]
    start = node.edge_start[first]
    if start < 0:
        return None
    count = node.edge_count[first]
    for j in range(count):
        edge = <CEdge>node.edges[start + j]
        child = _match_edge(edge, path, n, i, end, nxt)
        if child is not None:
            return child
    return None


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
        params[node.wildcard_name] = decoded
        return node.wildcard
    if node.catchall is not None:
        if node.catchall_name is not None:
            if rest != NULL and restlen >= 0:
                decoded = PyUnicode_DecodeUTF8(rest, restlen, "surrogatepass")
            else:
                decoded = PyUnicode_DecodeUTF8(seg, seglen, "surrogatepass")
            params[node.catchall_name] = decoded
        return node.catchall
    return None


cdef inline dict _params(dict params):
    if params is None:
        return {}
    return params


cdef object _finish(
    CNode node,
    object method,
    dict params,
    object nf_hit,
    object method_na,
    int* status,
):
    cdef object endpoints
    cdef object hit
    cdef object packed
    cdef object factory
    endpoints = node.endpoints
    if endpoints is None:
        status[0] = 2
        return nf_hit if nf_hit is not None else _NF_HIT
    if node.one_method is not None and method is node.one_method:
        hit = node.one_hit
    else:
        hit = endpoints.get(method)
    if hit is not None:
        status[0] = 0
        if params is not None:
            packed = <tuple>hit
            return _pack(
                packed[0],
                packed[1],
                _ROUTER_MATCH_CLS(packed[2].pattern, params),
            )
        return hit
    status[0] = 1
    if method_na is None and node.mna_hit is not None:
        return node.mna_hit
    factory = method_na or node.method_na or _ROUTER_DEFAULT_MNA
    return _pack(factory(node.method_set), _ROUTER_EMPTY_ROUTE, _ROUTER_EMPTY_MATCH)


cdef object _resolve_tree(
    CNode root,
    object path,
    object method,
    object host,
    int* status,
    bint* custom,
):
    cdef CNode node = root
    cdef CNode child
    cdef dict params = None
    cdef object nf_hit = root.nf_hit
    cdef object method_na = root.method_na
    cdef bint cust = root.not_found_custom
    cdef const char* path_p
    cdef const char* host_p = NULL
    cdef Py_ssize_t path_n
    cdef Py_ssize_t host_n = 0
    cdef object nf_before
    cdef bint custom_before
    cdef Py_ssize_t i
    cdef Py_ssize_t end
    cdef Py_ssize_t nxt
    cdef Py_ssize_t slash
    cdef Py_ssize_t dot
    cdef Py_ssize_t seg_start
    cdef Py_ssize_t unused = 0
    path_p = PyUnicode_AsUTF8AndSize(path, &path_n)
    if host:
        host_p = PyUnicode_AsUTF8AndSize(host, &host_n)
        nf_before = nf_hit
        custom_before = cust
        end = host_n
        while end > 0:
            dot = end - 1
            while dot >= 0 and host_p[dot] != 46:
                dot -= 1
            seg_start = dot + 1
            child = _match_exact(node, host_p, host_n, seg_start, end, &unused)
            if child is None:
                params = _params(params)
                if node.catchall is not None:
                    child = _take_param(
                        node,
                        host_p + seg_start,
                        end - seg_start,
                        host_p,
                        end,
                        params,
                    )
                else:
                    child = _take_param(
                        node, host_p + seg_start, end - seg_start, NULL, -1, params
                    )
                if child is None:
                    status[0] = 2
                    custom[0] = custom_before
                    return nf_before if nf_before is not None else _NF_HIT
                if child is node.catchall:
                    end = 0
                elif dot >= 0:
                    end = dot
                else:
                    end = 0
            else:
                end = dot if dot >= 0 else 0
            if child.not_found is not None:
                nf_hit = child.nf_hit
                cust = True
            if child.method_na is not None:
                method_na = child.method_na
            node = child
    nf_before = nf_hit
    custom_before = cust
    if not (path_n == 1 and path_p[0] == 47):
        i = 1
        while i <= path_n:
            if i == path_n:
                if path_p[path_n - 1] != 47:
                    break
                end = path_n
                nxt = path_n + 1
            else:
                slash = i
                while slash < path_n and path_p[slash] != 47:
                    slash += 1
                end = slash
                nxt = end + 1
            child = _match_exact(node, path_p, path_n, i, end, &nxt)
            if child is None:
                params = _params(params)
                if node.catchall is not None:
                    child = _take_param(
                        node,
                        path_p + i,
                        end - i,
                        path_p + i,
                        path_n - i,
                        params,
                    )
                else:
                    child = _take_param(
                        node, path_p + i, end - i, NULL, -1, params
                    )
                if child is None:
                    status[0] = 2
                    custom[0] = custom_before
                    return nf_before if nf_before is not None else _NF_HIT
                if child is node.catchall:
                    nxt = path_n + 1
            if child.not_found is not None:
                nf_hit = child.nf_hit
                cust = True
            if child.method_na is not None:
                method_na = child.method_na
            node = child
            i = nxt
    custom[0] = cust
    return _finish(node, method, params, nf_hit, method_na, status)


cdef object _router_lookup(CRouter self, object host, object path, object method):
    cdef object host_hit
    cdef object path_hit
    cdef object hroot
    cdef int host_status
    cdef int path_status
    cdef bint host_custom
    cdef bint path_custom
    if host is None:
        host = ""
    if self.host_routing and host:
        hroot = self.hosts_exact.get(host)
        if hroot is not None:
            host_hit = _resolve_tree(
                <CNode>hroot, path, method, "", &host_status, &host_custom
            )
        elif self.has_param_hosts:
            host_hit = _resolve_tree(
                self.hosts_param, path, method, host, &host_status, &host_custom
            )
        else:
            host_hit = _NF_HIT
            host_status = 2
            host_custom = False
        if host_status == 0:
            return host_hit
        path_hit = _resolve_tree(
            self.path, path, method, "", &path_status, &path_custom
        )
        if path_status == 0:
            return path_hit
        if host_status == 1:
            return host_hit
        if path_status == 1:
            return path_hit
        if host_custom:
            return host_hit
        return path_hit
    return _resolve_tree(self.path, path, method, "", &path_status, &path_custom)


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


cpdef CRouter compile_router(object router):
    cdef CRouter compiled = CRouter.__new__(CRouter)
    cdef dict hosts
    cdef object host
    cdef object tree
    _router_symbols()
    compiled.host_routing = router._host_routing
    compiled.has_param_hosts = router._has_param_hosts
    compiled.path = _compile_node(router._path)
    compiled.hosts_param = _compile_node(router._hosts_param)
    hosts = {}
    for host, tree in router._hosts_exact.items():
        hosts[host] = _compile_node(tree)
    compiled.hosts_exact = hosts
    return compiled
