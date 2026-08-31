# cython: language_level=3, boundscheck=False, wraparound=False
"""Tight loops for query index vs scan vs eager parse-all."""

from stario_cython.exchange import ParsedQuery

# 0 = production index get, 1 = previous scan-on-get, 2 = eager Python lists
MODE_INDEX = 0
MODE_SCAN = 1
MODE_EAGER = 2


cdef object _one_get(object q, object key, int mode):
    if mode == MODE_EAGER:
        return q._get_eager(key)
    if mode == MODE_SCAN:
        return q._get_scan(key)
    return q.get(key)


cdef list _one_getlist(object q, object key, int mode):
    if mode == MODE_EAGER:
        return q._getlist_eager(key)
    if mode == MODE_SCAN:
        return q._getlist_scan(key)
    return q.getlist(key)


def run_one_get(raw, key, Py_ssize_t iterations, int mode):
    cdef Py_ssize_t i
    cdef object q = ParsedQuery(b"")
    cdef object got
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        got = _one_get(q, key, mode)
        if got is not None:
            acc += len(<str>got)
    return acc


def run_many_gets(raw, keys, Py_ssize_t iterations, int mode):
    cdef Py_ssize_t i
    cdef Py_ssize_t j
    cdef Py_ssize_t k = len(keys)
    cdef object q = ParsedQuery(b"")
    cdef object got
    cdef object key
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        for j in range(k):
            key = keys[j]
            if mode == MODE_EAGER and j > 0:
                got = q.get(key)
            else:
                got = _one_get(q, key, mode)
            if got is not None:
                acc += len(<str>got)
    return acc


def run_getlist(raw, key, Py_ssize_t iterations, int mode):
    cdef Py_ssize_t i
    cdef object q = ParsedQuery(b"")
    cdef list got
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        got = _one_getlist(q, key, mode)
        acc += len(got)
    return acc
