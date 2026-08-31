"""
Application object: route table, shutdown-aware tasks, thin test entrypoint.

The HTTP protocol does not call this class per request. It uses
`find_handler` and `create_task(handler(c, w))`. `App.__call__` exists so
tests and `TestClient` share that same path.
"""

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.context import Context
from stario.http.invoke import finish_request_span, on_handler_done
from stario.telemetry.spans import NoOpSpan

from .dispatch import Router
from .writer import Writer


class App(Router):
    """Route table plus task tracking for graceful shutdown.

    Handlers are `async def` and must write a complete response. Uncaught
    exceptions are logged; if nothing was sent, the framework writes 500.
    A response already on the wire is not rewritten. Use `catch_errors` middleware
    or write error responses in handlers. Use `create_task` for work tied to a running server
    so drain can observe it.
    """

    def __init__(self) -> None:
        """Create an application (a `Router` with tracked tasks).

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

    @property
    def shutting_down(self) -> bool:
        """`True` once the runner has started draining this app."""
        return self.shutdown.done()

    def signal_shutdown(self) -> None:
        """Complete the shutdown future if still pending."""
        if not self.shutdown.done():
            self.shutdown.set_result(None)

    def create_task[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        name: str | None = None,
        eager_start: bool = False,
    ) -> asyncio.Task[T]:
        """Schedule a coroutine on the running loop and retain the task until it completes.

        The HTTP protocol schedules each request handler through this method so
        graceful shutdown can await in-flight work. App code can use the same
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

    async def __call__(self, c: Context, w: Writer) -> None:
        """Test / `TestClient` entrypoint: `find_handler` then the handler coroutine.

        Trailing-slash 308 is a protocol concern (Cython writes it inline). Tests
        that go through `App.__call__` get the same redirect here so they stay
        honest without a shared helper.
        """
        path = c.req.path
        if path != "/" and path.endswith("/"):
            target = "/" + path.strip("/")
            if c.req.query_bytes:
                target = f"{target}?{c.req.query_bytes.decode('latin-1')}"
            responses.redirect(w, target, 308)
            finish_request_span(
                c.span, status=308, method=c.req.method, path=path
            )
            return

        host = c.req.host if self.host_routing else ""
        handler, c.route = self.find_handler(host, path, c.req.method)
        span = c.span
        if type(span) is not NoOpSpan:
            span.start()
            span.attrs({"request.method": c.req.method, "request.path": path})

        task = self.create_task(handler(c, w), eager_start=True)
        try:
            if not task.done():
                await task
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise
        finally:
            if task.done():
                on_handler_done(c, w, task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not w.started:
            raise exc
