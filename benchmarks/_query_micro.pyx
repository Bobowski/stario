# cython: language_level=3, boundscheck=False, wraparound=False
"""Tight loops for query index vs eager parse-all."""

from stario_cython.exchange import ParsedQuery


def run_one_get(raw, key, Py_ssize_t iterations, bint eager):
    cdef Py_ssize_t i
    cdef object q = ParsedQuery(b"")
    cdef object got
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        if eager:
            got = q._get_eager(key)
        else:
            got = q.get(key)
        if got is not None:
            acc += len(<str>got)
    return acc


def run_many_gets(raw, keys, Py_ssize_t iterations, bint eager):
    cdef Py_ssize_t i
    cdef Py_ssize_t j
    cdef Py_ssize_t k = len(keys)
    cdef object q = ParsedQuery(b"")
    cdef object got
    cdef object key
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        if eager:
            for j in range(k):
                key = keys[j]
                if j == 0:
                    got = q._get_eager(key)
                else:
                    got = q.get(key)
                if got is not None:
                    acc += len(<str>got)
        else:
            for j in range(k):
                key = keys[j]
                got = q.get(key)
                if got is not None:
                    acc += len(<str>got)
    return acc


def run_getlist(raw, key, Py_ssize_t iterations, bint eager):
    cdef Py_ssize_t i
    cdef object q = ParsedQuery(b"")
    cdef list got
    cdef Py_ssize_t acc = 0
    for i in range(iterations):
        q.__init__(raw)
        if eager:
            got = q._getlist_eager(key)
        else:
            got = q.getlist(key)
        acc += len(got)
    return acc
