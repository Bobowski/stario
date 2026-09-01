# cython: language_level=3, boundscheck=False, wraparound=False
"""Tight loops for production ParsedQuery.get / getlist."""

from stario_cython.exchange import ParsedQuery


def run_one_get(raw, key, Py_ssize_t iterations):
    cdef Py_ssize_t i
    cdef object q = ParsedQuery(b"")
    cdef object got
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        got = q.get(key)
        if got is not None:
            acc += len(<str>got)
    return acc


def run_many_gets(raw, keys, Py_ssize_t iterations):
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
            got = q.get(key)
            if got is not None:
                acc += len(<str>got)
    return acc


def run_getlist(raw, key, Py_ssize_t iterations):
    cdef Py_ssize_t i
    cdef object q = ParsedQuery(b"")
    cdef list got
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        got = q.getlist(key)
        acc += len(got)
    return acc
