"""
Application object: route table, shutdown-aware tasks, thin test entrypoint.

The HTTP protocol does not call this class per request. It uses
`find_handler` (from `Router`) and `schedule_request` to run the matched
handler as a task. `App.__call__` exists so tests and `TestClient` share
that same schedule/finish path.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from stario.exceptions import StarioError
from stario.http.context import Context
from stario.http.invoke import schedule_request

from .dispatch import Router
from .writer import Writer


class App(Router):
    """Route table plus task tracking for graceful shutdown.

    Handlers are `async def`. Uncaught `HttpException`, `RedirectException`,
    and `ClientDisconnected` become those HTTP outcomes; anything else is 500.
    Use `create_task` for work tied to a running server so drain can observe it.
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
        """Test / `TestClient` entrypoint: same schedule as the HTTP protocol.

        Protocols should call `schedule_request` (or `find_handler` + `create_task`)
        directly so the handler coroutine is the task body.
        """
        task = schedule_request(self, c, w, eager_start=True)
        if task.done():
            return
        try:
            await task
        except Exception:
            return
