"""Tests for server-managed request tasks and shutdown handling."""

import asyncio
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from io import StringIO
from typing import Any

from stario.http.server import Server
from stario.http.types import Context
from stario.telemetry import JsonTracer
from stario.testing import TestRequest


@asynccontextmanager
async def bootstrap(app: Any, span: Any):
    yield


def make_server(*, graceful_timeout: float) -> Server:
    return Server(bootstrap, JsonTracer(StringIO()), graceful_timeout=graceful_timeout)


def make_context(tasks: set[asyncio.Task[Any]]) -> Context:
    tracer = JsonTracer(StringIO())
    return Context(
        app=object(),
        req=TestRequest(),
        span=tracer.create("request"),
        state={},
        create_task=lambda coro, name=None: track_task(tasks, coro, name=name),
    )


def track_task[T](
    tasks: set[asyncio.Task[Any]],
    coro: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
) -> asyncio.Task[T]:
    task = asyncio.create_task(coro, name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


class TestContextCreateTask:
    async def test_registers_task_and_removes_it_after_completion(self) -> None:
        tasks: set[asyncio.Task[Any]] = set()
        context = make_context(tasks)

        async def worker() -> int:
            await asyncio.sleep(0)
            return 42

        task = context.create_task(worker(), name="managed-worker")

        assert task in tasks
        assert task.get_name() == "managed-worker"
        assert await task == 42

        await asyncio.sleep(0)
        assert not tasks


class TestServerShutdownTasks:
    async def test_waits_for_managed_tasks_to_finish_within_grace_period(self) -> None:
        server = make_server(graceful_timeout=0.2)
        tasks: set[asyncio.Task[Any]] = set()
        finished = asyncio.Event()

        async def worker() -> None:
            await asyncio.sleep(0.02)
            finished.set()

        track_task(tasks, worker())

        start = asyncio.get_running_loop().time()
        await server._wait_for_managed_work_to_drain(set(), tasks)
        elapsed = asyncio.get_running_loop().time() - start

        assert finished.is_set()
        assert elapsed < 0.2

    async def test_cancels_managed_tasks_after_graceful_timeout(self) -> None:
        server = make_server(graceful_timeout=0.01)
        tasks: set[asyncio.Task[Any]] = set()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = track_task(tasks, worker())
        await started.wait()

        await server._wait_for_managed_work_to_drain(set(), tasks)
        cancelled_count = await server._cancel_pending_tasks(tasks)

        assert cancelled_count == 1
        assert cancelled.is_set()
        assert task.cancelled()

        await asyncio.sleep(0)
        assert not tasks
