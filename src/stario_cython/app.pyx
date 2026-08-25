# cython: language_level=3
"""Cython App: compiled create_task + request entry, on the Cython Router trie."""

import asyncio
from functools import lru_cache

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

from stario_cython.router cimport Router

cdef object NOOP_SPAN_TYPE = NoOpSpan


async def _default_http_exception(_c, w, exc):
    responses.text(w, exc.detail or "Error", exc.status_code)


async def _default_redirect_exception(_c, w, exc):
    responses.redirect(w, exc.location, exc.status_code)


async def _default_client_disconnected(_c, w, _exc):
    w.abort()


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

        self._loop = loop
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
        if eager_start:
            task = loop.create_task(coro, name=name, eager_start=True)
        else:
            task = loop.create_task(coro, name=name)
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

    async def __call__(self, object c, object w):
        cdef object span = c.span
        cdef bint traced = type(span) is not NOOP_SPAN_TYPE
        cdef object req = c.req
        cdef object path = req.path
        cdef object method = req.method
        cdef object host
        cdef object handler
        cdef object route
        cdef object target
        cdef object query_bytes
        cdef object exc
        cdef object err_handler
        cdef bint failed_after_start = False
        cdef bint handler_responded

        if traced:
            span.start()
            span.attrs({"request.method": method, "request.path": path})

        try:
            if path != "/" and path.endswith("/"):
                target = normalize_path(path)
                query_bytes = req.query_bytes
                if query_bytes:
                    target = f"{target}?{query_bytes.decode('latin-1')}"
                responses.redirect(w, target, 308)
                return

            host = req.host if self._has_hosts else ""
            handler, route = self.find_handler(host, path, method)
            c.route = route
            await handler(c, w)
            if not w.started and not w.completed:
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
            failed_after_start = w.started
            if not w.started:
                err_handler = self._find_error_handler(type(exc))
                if err_handler is not None:
                    try:
                        await err_handler(c, w, exc)
                        handler_responded = w.started or w.completed
                    except Exception as handler_exc:
                        failed_after_start = w.started
                        exc = handler_exc
                if not handler_responded:
                    responses.text(w, "Internal Server Error", 500)
            if not handler_responded:
                if traced:
                    span.fail(str(exc))
                    span.exception(exc)
        finally:
            if failed_after_start and not w.completed:
                w.abort()
            else:
                w.end()
            if traced:
                span.attr("response.status_code", w.status_code)
                span.end()


__all__ = ["App"]
