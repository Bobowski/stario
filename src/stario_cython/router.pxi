"""Compiled path/host trie. One node per segment; walk UTF-8 bytes.

Lookup takes the raw request path (as sent, still percent-encoded). It is
split on ``/`` first, then each segment is percent-decoded on its own, so an
encoded ``%2F`` is data inside one segment and never adds structure. Route
literals compare against decoded segments; params are fully decoded
(``%2F`` -> ``/``). Paths without ``%`` are walked straight from the arena
bytes, so static GET never allocates.

No radix ``rest`` compression (insert order cannot change the tree), no
exact-map sidecar, no backtracking: at each segment an exact child wins,
then ``{param}``, then ``{path...}``. Static hits return the 3-tuple stored at
compile time (same Match identity). Param hits allocate one Match + one
params dict. 404/405 tuples are interned on the node.
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
    cdef object endpoints
    cdef object method
    cdef object endpoint
    cdef object hit
    cdef object match
    cdef bytes key_b
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
    for key, child in node.exact.items():
        edge = CEdge.__new__(CEdge)
        key_b = key.encode("utf-8") if not isinstance(key, bytes) else key
        edge.key = key_b
        edge.key_n = PyBytes_GET_SIZE(key_b)
        edge.key_p = PyBytes_AS_STRING(key_b)
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


cdef inline CNode _match_edge(CEdge edge, const char* seg, Py_ssize_t seglen):
    if edge.key_n != seglen:
        return None
    if seglen and memcmp(edge.key_p, seg, <size_t>seglen) != 0:
        return None
    return edge.child


cdef CNode _match_exact(CNode node, const char* seg, Py_ssize_t seglen):
    cdef CEdge edge
    cdef int first
    cdef int start
    cdef int count
    cdef int j
    cdef CNode child
    if node.n_edges == 1:
        return _match_edge(node.one_edge, seg, seglen)
    if seglen == 0:
        first = 0
    else:
        first = <int><unsigned char>seg[0]
    start = node.edge_start[first]
    if start < 0:
        return None
    count = node.edge_count[first]
    for j in range(count):
        edge = <CEdge>node.edges[start + j]
        child = _match_edge(edge, seg, seglen)
        if child is not None:
            return child
    return None


cdef object _decode_param(const char* s, Py_ssize_t n):
    cdef object out
    try:
        out = PyUnicode_DecodeUTF8(s, n, NULL)
    except UnicodeDecodeError:
        return None
    return out


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
        decoded = _decode_param(seg, seglen)
        if decoded is None:
            return None
        params[node.wildcard_name] = decoded
        return node.wildcard
    if node.catchall is not None:
        if node.catchall_name is not None:
            if rest != NULL and restlen >= 0:
                decoded = _decode_param(rest, restlen)
            else:
                decoded = _decode_param(seg, seglen)
            if decoded is None:
                return None
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


cdef enum:
    SEG_STACK = 32
    DECODE_STACK = 512


cdef object _resolve_tree_n(
    CNode root,
    const char* path_p,
    Py_ssize_t path_n,
    bint decode,
    const char* host_p,
    Py_ssize_t host_n,
    object method,
    int* status,
    bint* custom,
):
    """Walk ``root`` for a raw path. ``decode`` means the path contains ``%``."""
    cdef CNode node = root
    cdef CNode child
    cdef dict params = None
    cdef object nf_hit = root.nf_hit
    cdef object method_na = root.method_na
    cdef bint cust = root.not_found_custom
    cdef object nf_before
    cdef bint custom_before
    cdef Py_ssize_t end
    cdef Py_ssize_t dot
    cdef Py_ssize_t seg_start
    cdef Py_ssize_t i
    cdef Py_ssize_t k
    cdef Py_ssize_t nseg = 0
    cdef Py_ssize_t cap
    cdef Py_ssize_t w
    cdef Py_ssize_t dn
    cdef Py_ssize_t seg_stack_off[SEG_STACK]
    cdef Py_ssize_t seg_stack_len[SEG_STACK]
    cdef Py_ssize_t* seg_off = seg_stack_off
    cdef Py_ssize_t* seg_len = seg_stack_len
    cdef char decode_stack[DECODE_STACK]
    cdef char* dbuf = NULL
    cdef const char* d
    cdef object result
    if path_p == NULL:
        path_p = ""
        path_n = 0
    if host_n > 0 and host_p != NULL:
        nf_before = nf_hit
        custom_before = cust
        end = host_n
        while end > 0:
            dot = end - 1
            while dot >= 0 and host_p[dot] != 46:
                dot -= 1
            seg_start = dot + 1
            child = _match_exact(node, host_p + seg_start, end - seg_start)
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
    if path_n == 0 or (path_n == 1 and path_p[0] == 47):
        custom[0] = cust
        return _finish(node, method, params, nf_hit, method_na, status)
    # Segments of the raw path, split on raw '/' only. ``d`` holds the bytes
    # the trie compares: the arena itself, or each segment decoded and joined
    # with '/' (a decoded %2F stays inside its segment).
    cap = 1
    for i in range(path_n):
        if path_p[i] == 47:
            cap += 1
    try:
        if cap > SEG_STACK:
            seg_off = <Py_ssize_t*>malloc(<size_t>cap * sizeof(Py_ssize_t))
            seg_len = <Py_ssize_t*>malloc(<size_t>cap * sizeof(Py_ssize_t))
            if seg_off == NULL or seg_len == NULL:
                raise MemoryError()
        if decode:
            if path_n <= DECODE_STACK:
                dbuf = decode_stack
            else:
                dbuf = <char*>malloc(<size_t>path_n)
                if dbuf == NULL:
                    raise MemoryError()
        i = 1 if path_p[0] == 47 else 0
        w = 0
        while True:
            seg_start = i
            while i < path_n and path_p[i] != 47:
                i += 1
            if decode:
                dn = _pct_decode_into(path_p + seg_start, i - seg_start, dbuf + w)
                if dn < 0:
                    status[0] = 2
                    custom[0] = custom_before
                    return nf_before if nf_before is not None else _NF_HIT
                seg_off[nseg] = w
                seg_len[nseg] = dn
                w += dn
                if i < path_n:
                    dbuf[w] = 47
                    w += 1
            else:
                seg_off[nseg] = seg_start
                seg_len[nseg] = i - seg_start
            nseg += 1
            if i >= path_n:
                break
            i += 1
        if decode:
            d = dbuf
            dn = w
        else:
            d = path_p
            dn = path_n
        for k in range(nseg):
            child = _match_exact(node, d + seg_off[k], seg_len[k])
            if child is None:
                params = _params(params)
                if node.catchall is not None:
                    child = _take_param(
                        node,
                        d + seg_off[k],
                        seg_len[k],
                        d + seg_off[k],
                        dn - seg_off[k],
                        params,
                    )
                else:
                    child = _take_param(
                        node, d + seg_off[k], seg_len[k], NULL, -1, params
                    )
                if child is None:
                    status[0] = 2
                    custom[0] = custom_before
                    return nf_before if nf_before is not None else _NF_HIT
            if child.not_found is not None:
                nf_hit = child.nf_hit
                cust = True
            if child.method_na is not None:
                method_na = child.method_na
            if child is node.catchall:
                node = child
                break
            node = child
        custom[0] = cust
        result = _finish(node, method, params, nf_hit, method_na, status)
        return result
    finally:
        if seg_off != seg_stack_off:
            free(seg_off)
        if seg_len != seg_stack_len:
            free(seg_len)
        if dbuf != NULL and dbuf != decode_stack:
            free(dbuf)


cdef object _router_lookup_n(
    CRouter self,
    object host,
    const char* path_p,
    Py_ssize_t path_n,
    bint decode,
    object method,
):
    cdef object host_hit
    cdef object path_hit
    cdef object hroot
    cdef const char* host_p = NULL
    cdef Py_ssize_t host_n = 0
    cdef int host_status
    cdef int path_status
    cdef bint host_custom
    cdef bint path_custom
    if host is None:
        host = ""
    if self.host_routing and host:
        host_p = PyUnicode_AsUTF8AndSize(host, &host_n)
        hroot = self.hosts_exact.get(host)
        if hroot is not None:
            host_hit = _resolve_tree_n(
                <CNode>hroot, path_p, path_n, decode, NULL, 0, method,
                &host_status, &host_custom,
            )
        elif self.has_param_hosts:
            host_hit = _resolve_tree_n(
                self.hosts_param, path_p, path_n, decode, host_p, host_n, method,
                &host_status, &host_custom,
            )
        else:
            host_hit = _NF_HIT
            host_status = 2
            host_custom = False
        if host_status == 0:
            return host_hit
        path_hit = _resolve_tree_n(
            self.path, path_p, path_n, decode, NULL, 0, method,
            &path_status, &path_custom,
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
    return _resolve_tree_n(
        self.path, path_p, path_n, decode, NULL, 0, method,
        &path_status, &path_custom,
    )


@cython.final
cdef class CEdge:
    pass


@cython.final
cdef class CNode:
    pass


@cython.final
cdef class CRouter:
    def lookup(self, host, path, method):
        """``path`` is the request path as sent (percent-encoded, no query)."""
        return self.c_lookup(host, path, method)

    cdef object c_lookup(self, object host, object path, object method):
        cdef const char* path_p
        cdef Py_ssize_t path_n
        cdef bint decode = False
        cdef Py_ssize_t i
        _router_symbols()
        if path is None:
            path = ""
        path_p = PyUnicode_AsUTF8AndSize(path, &path_n)
        for i in range(path_n):
            if path_p[i] == 37:
                decode = True
                break
        return _router_lookup_n(self, host, path_p, path_n, decode, method)

    cdef void c_lookup_into(
        self,
        object host,
        const char* path_p,
        Py_ssize_t path_n,
        bint decode,
        object method,
        RequestExchange exchange,
    ):
        cdef object hit
        _router_symbols()
        hit = _router_lookup_n(self, host, path_p, path_n, decode, method)
        exchange._handler = (<tuple>hit)[0]
        exchange._route = (<tuple>hit)[1]
        exchange.match = (<tuple>hit)[2]


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
