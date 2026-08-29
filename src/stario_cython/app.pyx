# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""Cython App: compiled request entry on the Cython Router trie.

``__call__`` is the protocol entrypoint. When the context is a
``RequestExchange`` (Cython HTTP server), path/method/started/completed are
read from cdef fields instead of Python properties. The Python httptools
server and tests still go through the generic attribute path.
"""

import asyncio
from functools import lru_cache
from inspect import isawaitable as _isawaitable

from cpython.unicode cimport PyUnicode_GET_LENGTH, PyUnicode_READ_CHAR

import stario.responses as responses
from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    RedirectException,
    StarioError,
    StarioRuntime,
)
from stario.routing.locations import normalize_path
from stario.telemetry.spans import NoOpSpan

from stario_cython.exchange cimport Request, RequestExchange
from stario_cython.router cimport Router

cdef object NOOP_SPAN_TYPE = NoOpSpan
cdef object EXCHANGE_TYPE = RequestExchange
cdef object EMPTY_HOST = ""


def _default_http_exception(_c, w, exc):
    responses.text(w, exc.detail or "Error", exc.status_code)


def _default_redirect_exception(_c, w, exc):
    responses.redirect(w, exc.location, exc.status_code)


def _default_client_disconnected(_c, w, _exc):
    w.abort()


cdef inline bint _writer_started(bint is_ex, RequestExchange ex, object w):
    if is_ex:
        return ex._status_code >= 0
    return w.started


cdef inline bint _writer_completed(bint is_ex, RequestExchange ex, object w):
    if is_ex:
        return ex._completed
    return w.completed


cdef void _finish_writer(
    bint is_ex,
    RequestExchange ex,
    object w,
    bint failed_after_start,
    bint traced,
    object span,
):
    cdef bint completed = _writer_completed(is_ex, ex, w)
    if failed_after_start and not completed:
        w.abort()
    elif not completed:
        w.end()
    if traced:
        if is_ex:
            span.attr("response.status_code", ex.status_code)
        else:
            span.attr("response.status_code", w.status_code)
        span.end()


cdef class App(Router):
    """Concrete app type: everything on `Router` plus errors and shutdown-aware tasks."""

    def __init__(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "App() requires a running event loop",
                help_text="Create App inside serve(), bootstrap, or async test code.",
            ) from exc

        self.shutdown = loop.create_future()
        self.tasks = set()
        self._task_discard = self.tasks.discard
        self._error_handlers = {
            HttpException: _default_http_exception,
            RedirectException: _default_redirect_exception,
            ClientDisconnected: _default_client_disconnected,
        }

        @lru_cache(maxsize=64)
        def find_handler(exc_type):
            for t in exc_type.__mro__:
                if t is Exception:
                    return None
                handler = self._error_handlers.get(t)
                if handler is not None:
                    return handler
            return None

        self._find_error_handler = find_handler

    @property
    def shutting_down(self):
        return self.shutdown.done()

    def signal_shutdown(self):
        if not self.shutdown.done():
            self.shutdown.set_result(None)

    def on_error(self, exc_type, handler):
        self._error_handlers[exc_type] = handler
        self._find_error_handler.cache_clear()

    def create_task(self, coro, *, loop=None, name=None, eager_start=False):
        """Schedule a coroutine on the running loop and retain the task until it completes."""
        cdef object task
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                raise StarioError(
                    "app.create_task() requires a running event loop",
                    help_text="Call app.create_task() from async code while the app is running.",
                ) from exc
        task = asyncio.Task(
            coro,
            loop=loop,
            name=name,
            eager_start=eager_start,
        )
        if not task.done():
            self.tasks.add(task)
            task.add_done_callback(self._task_discard)
        return task

    async def drain_tasks(self):
        while True:
            pending = set(self.tasks)
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)

    async def _continue_async(
        self,
        object c,
        object w,
        object pending,
        bint is_ex,
        RequestExchange ex,
        bint traced,
        object span,
        object method,
        object path,
        object route,
    ):
        cdef object err_handler
        cdef object err_result
        cdef bint failed_after_start = False
        cdef bint handler_responded
        cdef bint started
        try:
            await pending
            if not _writer_started(is_ex, ex, w) and not _writer_completed(is_ex, ex, w):
                raise StarioRuntime(
                    "Handler returned without sending a response",
                    context={
                        "method": method,
                        "path": path,
                        "route": route.pattern or None,
                    },
                    help_text=(
                        "Call a response helper such as responses.text/json/html/empty, "
                        "or explicitly use Writer.write_headers()/write()/end()."
                    ),
                )
        except Exception as exc:
            handler_responded = False
            started = _writer_started(is_ex, ex, w)
            failed_after_start = started
            if not started:
                err_handler = self._find_error_handler(type(exc))
                if err_handler is not None:
                    try:
                        err_result = err_handler(c, w, exc)
                        if _isawaitable(err_result):
                            await err_result
                        handler_responded = (
                            _writer_started(is_ex, ex, w)
                            or _writer_completed(is_ex, ex, w)
                        )
                    except Exception as handler_exc:
                        failed_after_start = _writer_started(is_ex, ex, w)
                        exc = handler_exc
                if not handler_responded:
                    responses.text(w, "Internal Server Error", 500)
            if not handler_responded and traced:
                span.fail(str(exc))
                span.exception(exc)
        finally:
            _finish_writer(is_ex, ex, w, failed_after_start, traced, span)

    cpdef object dispatch(self, object c, object w):
        """Run one request. Returns None when finished, or a coroutine to Task.

        Sync handlers (``def plaintext(c, w)``) complete here with no
        ``asyncio.Task`` and no App coroutine. Async handlers return a
        continuation the protocol schedules with ``eager_start``.
        """
        cdef RequestExchange ex = None
        cdef Request req
        cdef object span
        cdef object path
        cdef object method
        cdef object host
        cdef object handler
        cdef object route
        cdef object target
        cdef object query_bytes
        cdef object err_handler
        cdef object result
        cdef object err_result
        cdef bint is_ex = type(c) is EXCHANGE_TYPE
        cdef bint traced
        cdef bint failed_after_start = False
        cdef bint handler_responded
        cdef bint started
        cdef Py_ssize_t n

        if is_ex:
            ex = <RequestExchange>c
            req = ex.req
            path = req.path
            method = req.method
            span = ex.span
        else:
            path = c.req.path
            method = c.req.method
            span = c.span

        traced = type(span) is not NOOP_SPAN_TYPE
        if traced:
            span.start()
            span.attrs({"request.method": method, "request.path": path})

        try:
            n = PyUnicode_GET_LENGTH(path)
            if n > 1 and PyUnicode_READ_CHAR(path, n - 1) == 47:
                target = normalize_path(path)
                if is_ex:
                    query_bytes = req.query_bytes
                else:
                    query_bytes = c.req.query_bytes
                if query_bytes:
                    target = f"{target}?{query_bytes.decode('latin-1')}"
                responses.redirect(w, target, 308)
            else:
                if not self._has_hosts:
                    host = EMPTY_HOST
                elif is_ex:
                    host = req.host
                else:
                    host = c.req.host
                handler, route = self.find_handler(host, path, method)
                if is_ex:
                    ex.route = route
                else:
                    c.route = route
                result = handler(c, w)
                if result is not None:
                    return self._continue_async(
                        c, w, result, is_ex, ex, traced, span, method, path, route
                    )
                if not _writer_started(is_ex, ex, w) and not _writer_completed(is_ex, ex, w):
                    raise StarioRuntime(
                        "Handler returned without sending a response",
                        context={
                            "method": method,
                            "path": path,
                            "route": route.pattern or None,
                        },
                        help_text=(
                            "Call a response helper such as responses.text/json/html/empty, "
                            "or explicitly use Writer.write_headers()/write()/end()."
                        ),
                    )
        except Exception as exc:
            handler_responded = False
            started = _writer_started(is_ex, ex, w)
            failed_after_start = started
            if not started:
                err_handler = self._find_error_handler(type(exc))
                if err_handler is not None:
                    try:
                        err_result = err_handler(c, w, exc)
                        if _isawaitable(err_result):
                            return self._continue_async(
                                c,
                                w,
                                err_result,
                                is_ex,
                                ex,
                                traced,
                                span,
                                method,
                                path,
                                getattr(c, "route", None),
                            )
                        handler_responded = (
                            _writer_started(is_ex, ex, w)
                            or _writer_completed(is_ex, ex, w)
                        )
                    except Exception as handler_exc:
                        failed_after_start = _writer_started(is_ex, ex, w)
                        exc = handler_exc
                if not handler_responded:
                    responses.text(w, "Internal Server Error", 500)
            if not handler_responded and traced:
                span.fail(str(exc))
                span.exception(exc)
        _finish_writer(is_ex, ex, w, failed_after_start, traced, span)
        return None

    cpdef object dispatch_exchange(self, RequestExchange ex):
        """RequestExchange hot path: no type check, no DummyWriter branches."""
        cdef Request req = ex.req
        cdef object span = ex.span
        cdef object path = req.path
        cdef object method = req.method
        cdef object host
        cdef object handler
        cdef object route
        cdef object target
        cdef object query_bytes
        cdef object err_handler
        cdef object result
        cdef object err_result
        cdef bint traced = type(span) is not NOOP_SPAN_TYPE
        cdef bint failed_after_start = False
        cdef bint handler_responded
        cdef bint started
        cdef Py_ssize_t n
        cdef object w = ex

        if traced:
            span.start()
            span.attrs({"request.method": method, "request.path": path})

        try:
            n = PyUnicode_GET_LENGTH(path)
            if n > 1 and PyUnicode_READ_CHAR(path, n - 1) == 47:
                target = normalize_path(path)
                query_bytes = req.query_bytes
                if query_bytes:
                    target = f"{target}?{query_bytes.decode('latin-1')}"
                responses.redirect(w, target, 308)
            else:
                if not self._has_hosts:
                    host = EMPTY_HOST
                else:
                    host = req.host
                handler, route = self.find_handler(host, path, method)
                ex.route = route
                result = handler(ex, w)
                if result is not None:
                    return self._continue_async(
                        ex, w, result, True, ex, traced, span, method, path, route
                    )
                if ex._status_code < 0 and not ex._completed:
                    raise StarioRuntime(
                        "Handler returned without sending a response",
                        context={
                            "method": method,
                            "path": path,
                            "route": route.pattern or None,
                        },
                        help_text=(
                            "Call a response helper such as responses.text/json/html/empty, "
                            "or explicitly use Writer.write_headers()/write()/end()."
                        ),
                    )
        except Exception as exc:
            handler_responded = False
            started = ex._status_code >= 0
            failed_after_start = started
            if not started:
                err_handler = self._find_error_handler(type(exc))
                if err_handler is not None:
                    try:
                        err_result = err_handler(ex, w, exc)
                        if _isawaitable(err_result):
                            return self._continue_async(
                                ex, w, err_result, True, ex, traced, span, method, path, route
                            )
                        handler_responded = ex._status_code >= 0 or ex._completed
                    except Exception as handler_exc:
                        failed_after_start = ex._status_code >= 0
                        exc = handler_exc
                if not handler_responded:
                    responses.text(w, "Internal Server Error", 500)
            if not handler_responded and traced:
                span.fail(str(exc))
                span.exception(exc)
        _finish_writer(True, ex, w, failed_after_start, traced, span)
        return None

    async def __call__(self, object c, object w):
        cdef object pending = self.dispatch(c, w)
        if pending is not None:
            await pending


__all__ = ["App"]
