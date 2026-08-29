from stario_cython.router cimport Router
from stario_cython.exchange cimport RequestExchange

cdef class App(Router):
    cdef public object shutdown
    cdef public object tasks
    cdef dict _error_handlers
    cdef object _find_error_handler
    cdef object _task_discard
    cpdef object dispatch(self, object c, object w)
    cpdef object dispatch_exchange(self, RequestExchange ex)
