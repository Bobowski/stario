from stario_cython.router cimport Router

cdef class App(Router):
    cdef public object shutdown
    cdef public object tasks
    cdef dict _error_handlers
    cdef object _find_error_handler
    cdef object _loop
    cdef object _task_discard
