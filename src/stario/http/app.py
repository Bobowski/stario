"""
Single entrypoint for request handling above the router: error surface, tracing, and `writer.end()` guarantees.

The protocol dispatches a callable; this class is where policy lives so the route trie stays a pure match/registration
structure. `create_task` registers work the server can wait on during shutdown—use it instead of orphan `asyncio.create_task` calls for request-adjacent work.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from functools import lru_cache
from inspect import isawaitable
from typing import Any

import stario.responses as responses
from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    RedirectException,
    StarioError,
    StarioRuntime,
)
from stario.http.context import Context
from stario.routing.locations import normalize_path
from stario.telemetry.spans import NoOpSpan

from .dispatch import Router
from .writer import Writer

type ErrorHandler[E: Exception] = Callable[[Context, Writer, E], Awaitable[None] | None]


def _default_http_exception(_c: Context, w: Writer, exc: HttpException) -> None:
    responses.text(w, exc.detail or "Error", exc.status_code)


def _default_redirect_exception(
    _c: Context, w: Writer, exc: RedirectException
) -> None:
    responses.redirect(w, exc.location, exc.status_code)


def _default_client_disconnected(
    _c: Context, w: Writer, _exc: ClientDisconnected
) -> None:
    w.abort()


class App(Router):
    """Concrete app type: everything on `Router` plus errors and shutdown-aware tasks.

    Uncaught exceptions become HTTP responses only before headers are sent; after that, telemetry still records the failure.
    Use `create_task` for work tied to a running server so graceful shutdown can observe it.
    """

    def __init__(self) -> None:
        """Create an application (a `Router` with error handling and tasks).

        Requires a running event loop — create inside `serve()`, bootstrap,
        or async test code. `shutdown` completes when the runner begins draining.
        """
        super().__init__()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "App() requires a running event loop",
                help_text="Create App inside serve(), bootstrap, or async test code.",
            ) from exc

        self.shutdown = loop.create_future()
        self.tasks: set[asyncio.Task[Any]] = set()
        self._error_handlers: dict[type[Exception], ErrorHandler[Any]] = {
            HttpException: _default_http_exception,
            RedirectException: _default_redirect_exception,
            ClientDisconnected: _default_client_disconnected,
        }

        @lru_cache(maxsize=64)
        def find_handler(exc_type: type[Exception]) -> ErrorHandler[Any] | None:
            # Most-specific registered type wins by walking the MRO.
            for t in exc_type.__mro__:
                if t is Exception:
                    return None
                if handler := self._error_handlers.get(t):
                    return handler
            return None

        self._find_error_handler = find_handler

    @property
    def shutting_down(self) -> bool:
        """`True` once the runner has started draining this app."""
        return self.shutdown.done()

    def signal_shutdown(self) -> None:
        """Complete the shutdown future if still pending."""
        if not self.shutdown.done():
            self.shutdown.set_result(None)

    # --- error handler registry ---

    def on_error(
        self, exc_type: type[Exception], handler: ErrorHandler[Exception]
    ) -> None:
        """Register a handler for uncaught exceptions of type `exc_type` (subclasses use MRO; most specific wins).

        - `exc_type`: Exception class to match.
        - `handler`: Callable receiving `(context, writer, exc)` (sync or async).

        Only runs while the writer has not started (`w.started` is false); after
        headers are sent, failures use `w.abort()` in the `finally` block.
        `HttpException`, `RedirectException`, and `ClientDisconnected` are
        registered by default.
        """
        self._error_handlers[exc_type] = handler
        self._find_error_handler.cache_clear()

    # --- background tasks (tracked until drain_tasks or server shutdown) ---

    def create_task[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        name: str | None = None,
        eager_start: bool = False,
    ) -> asyncio.Task[T]:
        """Schedule a coroutine on the running loop and retain the task until it completes.

        The HTTP protocol schedules each request's `App.__call__` through this
        method so graceful shutdown can await in-flight handlers. App code can use
        the same API for background work; both share `tasks` until shutdown drain.

        - `coro`: Coroutine to run.
        - `loop`: Optional loop to schedule on when the caller already has it.
        - `name`: Optional task name for debuggers.
        - `eager_start`: Run immediately until the first suspension.

        The new `asyncio.Task`.

        - `StarioError`: If no event loop is running (call from async request or app code only).
        """
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
            task.add_done_callback(self.tasks.discard)
        return task

    async def drain_tasks(self) -> None:
        """Await until every task created with `create_task` has finished (including nested scheduling).

        Useful in tests to wait for background work after the HTTP response has been sent. Do not call from
        inside a coroutine that is itself tracked in `create_task` or you risk deadlock.
        """
        while True:
            pending = set(self.tasks)
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)

    # --- protocol entrypoint (called once per HTTP request) ---

    def dispatch(self, c: Context, w: Writer) -> Coroutine[Any, Any, None] | None:
        """Run one request without wrapping sync handlers in `asyncio.Task`.

        Returns `None` when the handler finished immediately (plain `def`), or a
        coroutine the HTTP protocol should schedule when the handler is async.
        """
        return _dispatch(self, c, w)

    async def __call__(self, c: Context, w: Writer) -> None:
        """Protocol entrypoint: open a span, resolve routes, handle errors, and finish started responses.

        - `c`: Request context (`app`, `req`, `span`, `route`, `state`).
        - `w`: Response writer for this message on the connection.

        Trailing slashes (except `/`) get `308` to a canonical path (leading `/`, no trailing
        slash) before `find_handler` runs. Wrong method on a matching path yields `405`.

        Uncaught exceptions while headers are not sent: `HttpException` →
        `responses.text`, `RedirectException` → `responses.redirect`,
        `ClientDisconnected` → `w.abort()` (no body); anything else falls back
        to 500 unless `on_error` handled it. If a registered error handler
        raises, the request span is marked failed and 500 is sent when the
        handler did not start or complete the writer. Handlers must explicitly
        send a response on the success path.

        Sync handlers (`def plaintext(c, w)`) are supported: they run inline
        and do not need `await`.
        """
        pending = self.dispatch(c, w)
        if pending is not None:
            await pending


def _finish_writer(
    c: Context, w: Writer, failed_after_start: bool, record: bool
) -> None:
    if failed_after_start and not w.completed:
        w.abort()
    elif not w.completed:
        w.end()
    if record:
        c.span.attr("response.status_code", w.status_code)
        c.span.end()


async def _continue_async(
    app: App,
    c: Context,
    w: Writer,
    pending: Awaitable[None],
    record: bool,
) -> None:
    failed_after_start = False
    try:
        await pending
        if not w.started and not w.completed:
            raise StarioRuntime(
                "Handler returned without sending a response",
                context={
                    "method": c.req.method,
                    "path": c.req.path,
                    "route": c.route.pattern or None,
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
            if handler := app._find_error_handler(type(exc)):
                try:
                    err_result = handler(c, w, exc)
                    if isawaitable(err_result):
                        await err_result
                    handler_responded = w.started or w.completed
                except Exception as handler_exc:
                    failed_after_start = w.started
                    exc = handler_exc
            if not handler_responded:
                responses.text(w, "Internal Server Error", 500)
        if not handler_responded and record:
            c.span.fail(str(exc))
            c.span.exception(exc)
    finally:
        _finish_writer(c, w, failed_after_start, record)


def _dispatch(app: App, c: Context, w: Writer) -> Coroutine[Any, Any, None] | None:
    span = c.span
    record = type(span) is not NoOpSpan
    if record:
        span.start()
        span.attrs({"request.method": c.req.method, "request.path": c.req.path})
    failed_after_start = False

    try:
        path = c.req.path
        if path != "/" and path.endswith("/"):
            target = normalize_path(path)
            if c.req.query_bytes:
                target = f"{target}?{c.req.query_bytes.decode('latin-1')}"
            responses.redirect(w, target, 308)
        else:
            host = c.req.host if app.host_routing else ""
            handler, c.route = app.find_handler(host, path, c.req.method)
            result = handler(c, w)
            if isawaitable(result):
                return _continue_async(app, c, w, result, record)
            if not w.started and not w.completed:
                raise StarioRuntime(
                    "Handler returned without sending a response",
                    context={
                        "method": c.req.method,
                        "path": c.req.path,
                        "route": c.route.pattern or None,
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
            if handler := app._find_error_handler(type(exc)):
                try:
                    err_result = handler(c, w, exc)
                    if isawaitable(err_result):
                        return _continue_async(app, c, w, err_result, record)
                    handler_responded = w.started or w.completed
                except Exception as handler_exc:
                    failed_after_start = w.started
                    exc = handler_exc
            if not handler_responded:
                responses.text(w, "Internal Server Error", 500)
        if not handler_responded and record:
            span.fail(str(exc))
            span.exception(exc)
    _finish_writer(c, w, failed_after_start, record)
    return None
