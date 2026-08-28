cdef class Node:
    cdef public object not_found_handler
    cdef public object method_not_allowed_handler
    cdef public tuple middleware
    cdef public int host_depth
    cdef public dict exact
    cdef public object wildcard_name
    cdef public Node wildcard
    cdef public object catchall_name
    cdef public Node catchall
    cdef public dict endpoints
    cdef public object methods


cdef class Endpoint:
    cdef public object handler
    cdef public object route_match


cdef class Router:
    cdef Node _path
    cdef Node _hosts
    cdef bint _has_hosts
    cdef dict _static
    cdef dict _cache
    cdef int _cache_n
    cdef dict __dict__

    cpdef tuple find_handler(self, object host, object path, object method)
    cdef tuple _match(self, object host, object path, object method)
    cdef void _clear_match_cache(self)
    cdef Node _registration_tree(self, object pattern)
    cdef Node _leaf_node(self, object pattern)
    cdef Node _policy_node(self, object pattern)
    cdef void _remember_static(self, object pattern, object method, object result)
