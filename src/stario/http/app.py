"""
Single entrypoint for request handling above the router: error surface, tracing, and `writer.end()` guarantees.

The protocol calls `dispatch()` — sync handlers run immediately; async handlers are
scheduled with `create_task`. Policy lives here so the route trie stays a pure
match/registration structure. `create_task` registers work the server can wait on
during shutdown—use it instead of orphan `asyncio.create_task` calls for
request-adjacent work.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from functools import lru_cache
from typing import Any

import stario.responses as responses
from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    RedirectException,
    StarioError,
    StarioRuntime,
)
from stario.http.context import Context, Handler
from stario.routing.locations import normalize_path
from stario.telemetry.spans import NoOpSpan

from .dispatch import Router
from .writer import Writer

type ErrorHandler[E: Exception] = Callable[
    [Context, Writer, E], Awaitable[None] | None
]


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


def _redirect_trailing_slash(c: Context, w: Writer) -> None:
    target = normalize_path(c.req.path)
    if c.req.query_bytes:
        target = f"{target}?{c.req.query_bytes.decode('latin-1')}"
    responses.redirect(w, target, 308)


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
        - `handler`: Sync or async callable receiving `(context, writer, exc)`.

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

        The HTTP protocol schedules each async request through this method so
        graceful shutdown can await in-flight handlers. App code can use the same
        API for background work; both share `tasks` until shutdown drain.

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

    def dispatch(
        self,
        c: Context,
        w: Writer,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        eager_start: bool = False,
    ) -> asyncio.Task[None] | None:
        """Protocol contract: run one request.

        Sync handlers (and trailing-slash redirects) run immediately and return
        `None`. Async handlers are scheduled with `create_task` and the `Task`
        is returned so the protocol can attach a done callback.

        - `c`: Request context (`app`, `req`, `span`, `route`, `state`).
        - `w`: Response writer for this message on the connection.
        - `loop`: Optional loop when the caller already has it.
        - `eager_start`: For async handlers, run until the first suspension.
        """
        path = c.req.path
        if path != "/" and path.endswith("/"):
            return self._dispatch_sync(c, w, _redirect_trailing_slash)

        host = c.req.host if self.host_routing else ""
        handler, c.route, is_async = self._find_handler(host, path, c.req.method)
        if is_async:
            return self.create_task(
                self._dispatch_async(c, w, handler),
                loop=loop,
                eager_start=eager_start,
            )
        return self._dispatch_sync(c, w, handler)

    async def __call__(self, c: Context, w: Writer) -> None:
        """Awaitable entry: `dispatch()` then await the task when the handler is async.

        Tests and in-process clients use this. The HTTP protocol calls `dispatch`
        directly so sync handlers skip task creation.

        Trailing slashes (except `/`) get `308` to a canonical path (leading `/`, no trailing
        slash) before routing. Wrong method on a matching path yields `405`.

        Uncaught exceptions while headers are not sent: `HttpException` →
        `responses.text`, `RedirectException` → `responses.redirect`,
        `ClientDisconnected` → `w.abort()` (no body); anything else falls back
        to 500 unless `on_error` handled it. If a registered error handler
        raises, the request span is marked failed and 500 is sent when the
        handler did not start or complete the writer. Handlers must explicitly
        send a response on the success path.
        """
        task = self.dispatch(c, w, eager_start=True)
        if task is not None:
            await task

    def _begin_span(self, c: Context) -> tuple[Any, bool]:
        span = c.span
        # Cython (and benchmark) servers use NoOpTracer. Skip method calls and
        # the attrs dict on that path; RecordingSpan and others still record.
        record = type(span) is not NoOpSpan
        if record:
            span.start()
            span.attrs({"request.method": c.req.method, "request.path": c.req.path})
        return span, record

    def _ensure_response(self, c: Context, w: Writer) -> None:
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

    def _finish_request(
        self,
        w: Writer,
        span: Any,
        record: bool,
        *,
        failed_after_start: bool,
        exc: BaseException | None = None,
        handler_responded: bool = True,
    ) -> None:
        if failed_after_start and not w.completed:
            w.abort()
        elif not w.completed:
            w.end()
        if record:
            if exc is not None and not handler_responded:
                span.fail(str(exc))
                span.exception(exc)
            span.attr("response.status_code", w.status_code)
            span.end()

    def _dispatch_sync(
        self,
        c: Context,
        w: Writer,
        handler: Handler,
    ) -> asyncio.Task[None] | None:
        span, record = self._begin_span(c)
        failed_after_start = False
        pending: Awaitable[None] | None = None
        finish_exc: BaseException | None = None
        handler_responded = True
        try:
            result = handler(c, w)
            if inspect.isawaitable(result):
                raise StarioRuntime(
                    "Sync handler returned an awaitable",
                    context={
                        "method": c.req.method,
                        "path": c.req.path,
                        "route": c.route.pattern or None,
                    },
                    help_text="Declare the handler as `async def` if it needs await.",
                )
            self._ensure_response(c, w)
        except Exception as exc:
            handler_responded = False
            failed_after_start = w.started
            finish_exc = exc
            if not w.started:
                error_handler = self._find_error_handler(type(exc))
                if error_handler is not None:
                    try:
                        error_result = error_handler(c, w, exc)
                        if inspect.isawaitable(error_result):
                            pending = error_result
                        else:
                            handler_responded = w.started or w.completed
                    except Exception as handler_exc:
                        failed_after_start = w.started
                        finish_exc = handler_exc
                if pending is None and not handler_responded:
                    responses.text(w, "Internal Server Error", 500)
        finally:
            if pending is None:
                self._finish_request(
                    w,
                    span,
                    record,
                    failed_after_start=failed_after_start,
                    exc=finish_exc,
                    handler_responded=handler_responded,
                )
        if pending is not None:
            return self.create_task(
                self._finish_async_error(
                    w,
                    pending,
                    span,
                    record,
                    failed_after_start=failed_after_start,
                    exc=finish_exc,
                ),
                eager_start=True,
            )
        return None

    async def _dispatch_async(self, c: Context, w: Writer, handler: Handler) -> None:
        span, record = self._begin_span(c)
        failed_after_start = False
        finish_exc: BaseException | None = None
        handler_responded = True
        try:
            await handler(c, w)  # type: ignore[misc]
            self._ensure_response(c, w)
        except Exception as exc:
            handler_responded = False
            failed_after_start = w.started
            finish_exc = exc
            if not w.started:
                error_handler = self._find_error_handler(type(exc))
                if error_handler is not None:
                    try:
                        error_result = error_handler(c, w, exc)
                        if inspect.isawaitable(error_result):
                            await error_result
                        handler_responded = w.started or w.completed
                    except Exception as handler_exc:
                        failed_after_start = w.started
                        finish_exc = handler_exc
                if not handler_responded:
                    responses.text(w, "Internal Server Error", 500)
        finally:
            self._finish_request(
                w,
                span,
                record,
                failed_after_start=failed_after_start,
                exc=finish_exc,
                handler_responded=handler_responded,
            )

    async def _finish_async_error(
        self,
        w: Writer,
        pending: Awaitable[None],
        span: Any,
        record: bool,
        *,
        failed_after_start: bool,
        exc: BaseException | None,
    ) -> None:
        handler_responded = False
        finish_exc = exc
        try:
            await pending
            handler_responded = w.started or w.completed
        except Exception as handler_exc:
            failed_after_start = w.started
            finish_exc = handler_exc
        if not handler_responded:
            responses.text(w, "Internal Server Error", 500)
        self._finish_request(
            w,
            span,
            record,
            failed_after_start=failed_after_start,
            exc=finish_exc,
            handler_responded=handler_responded,
        )
