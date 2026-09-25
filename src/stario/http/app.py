"""
Application object: route table, shutdown-aware tasks, thin test entrypoint.

The HTTP protocol does not call this class per request. It binds compiled
trie lookup (`_cy_router`) and constructs `asyncio.Task(handler(c, w))`,
registering incomplete tasks on `app.tasks` for shutdown drain. `App.__call__`
and `create_task` exist so tests and `TestClient` share that same drain set.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Coroutine
from typing import Any

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.context import Context
from stario.http.invoke import finish_request_span, on_handler_done
from stario.http.route import normalize_path
from stario.telemetry.spans import NoOpSpan

from stario_cython.exchange import AppState

from .dispatch import Router
from .writer import Writer


def _complete_loop_future(fut: asyncio.Future[None]) -> None:
    if fut.done():
        return
    try:
        # Bypass `_LoopShutdown.set_result`, which fans out through signal_shutdown.
        asyncio.Future.set_result(fut, None)  # pyright: ignore[reportUnknownMemberType]
    except asyncio.InvalidStateError:
        return


class _LoopShutdown(asyncio.Future[None]):
    """Per-loop wait handle. `set_result` fans out through `App.signal_shutdown`."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop, app: App) -> None:
        super().__init__(loop=loop)
        self._stario_app = app

    def set_result(self, result: None) -> None:  # type: ignore[override]
        self._stario_app.signal_shutdown()


class App(Router):
    """Route table plus task tracking for graceful shutdown.

    Handlers are `async def` and must write a complete response. Uncaught
    exceptions are logged; if nothing was sent, the framework writes 500.
    A response already on the wire is not rewritten. Use `catch_errors` middleware
    or write error responses in handlers. Use `create_task` for work tied to a running server
    so drain can observe it.

    `shutdown` is the **current loop's** Future. `signal_shutdown()` completes
    every attached loop so worker threads wake together. `tasks` is per-thread
    so drain only sees work on this loop.
    """

    def __init__(self) -> None:
        """Create an application (a `Router` with tracked tasks).

        Requires a running event loop — create inside `serve()`, bootstrap,
        or async test code. `shutdown` completes when the runner begins draining.
        """
        super().__init__()
        self._app_state = AppState()
        self._app_state.host_routing = self._host_routing
        self._app_state.router = self._cy_router
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "App() requires a running event loop",
                help_text="Create App inside serve(), bootstrap, or async test code.",
            ) from exc

        self._shutdown_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._loop_shutdown: dict[asyncio.AbstractEventLoop, _LoopShutdown] = {}
        self._task_local = threading.local()
        self.attach_loop(loop)

    def attach_loop(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.Future[None]:
        """Register `loop` (default: running loop) so shutdown reaches it."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:
                raise StarioError(
                    "attach_loop() requires a running event loop",
                    help_text="Call App.attach_loop() from the worker thread's loop.",
                ) from exc
        return self._future_for(loop)

    def _future_for(self, loop: asyncio.AbstractEventLoop) -> _LoopShutdown:
        with self._shutdown_lock:
            fut = self._loop_shutdown.get(loop)
            if fut is None:
                fut = _LoopShutdown(loop=loop, app=self)
                self._loop_shutdown[loop] = fut
        if self._shutdown_event.is_set() and not fut.done():
            if loop.is_closed():
                return fut
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                _complete_loop_future(fut)
            else:
                try:
                    loop.call_soon_threadsafe(_complete_loop_future, fut)
                except RuntimeError:
                    if not loop.is_closed():
                        raise
        return fut

    @property
    def shutdown(self) -> asyncio.Future[None]:
        """This thread's shutdown Future — await it; `set_result` signals all loops."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "app.shutdown requires a running event loop",
                help_text="Await app.shutdown from async code on the loop that serves requests.",
            ) from exc
        return self._future_for(loop)

    @property
    def shutting_down(self) -> bool:
        """`True` once the runner has started draining this app."""
        return self._shutdown_event.is_set()

    def signal_shutdown(self) -> None:
        """Complete shutdown on every attached loop."""
        self._shutdown_event.set()
        self._app_state.shutting_down = True
        with self._shutdown_lock:
            items = list(self._loop_shutdown.items())
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for loop, fut in items:
            if fut.done():
                continue
            if running is loop:
                _complete_loop_future(fut)
                continue
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(_complete_loop_future, fut)
            except RuntimeError:
                if not loop.is_closed():
                    raise

    @property
    def tasks(self) -> set[asyncio.Task[Any]]:
        """Tasks scheduled via `create_task` on this OS thread."""
        stored: set[asyncio.Task[Any]] | None = getattr(self._task_local, "tasks", None)
        if stored is None:
            stored = set()
            self._task_local.tasks = stored
        return stored

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
            tasks = self.tasks
            tasks.add(task)
            task.add_done_callback(tasks.discard)
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
        host = c.req.host if self.host_routing else ""
        if path != "/" and path.endswith("/"):
            target = normalize_path(path)
            _, _, hit = self.find_handler(host, target, c.req.method)
            if type(c.span) is not NoOpSpan and hit.pattern:
                c.span.rename(hit.pattern)
            if c.req.query_bytes:
                target = f"{target}?{c.req.query_bytes.decode('latin-1')}"
            responses.redirect(w, target, 308)
            finish_request_span(c.span, status=308, method=c.req.method, path=path)
            return

        handler, route, c.match = self.find_handler(host, path, c.req.method)
        span = c.span
        if type(span) is not NoOpSpan:
            span.start()
            span.attrs({"request.method": c.req.method, "request.path": path})
            if c.match.pattern:
                span.rename(c.match.pattern)
                span.attr("http.route", route.path)

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
