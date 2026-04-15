"""Tests for server-managed request tasks and shutdown handling."""

import asyncio
import os
import stat
import tempfile
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from io import StringIO
from typing import Any, cast
from urllib.parse import urlencode

from stario import App
from stario.http.context import Context
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request
from stario.http.server import Server
from stario.telemetry import JsonTracer, Span


def _make_request(
    *,
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: dict[str, object] | None = None,
) -> Request:
    hdrs = Headers()
    if headers:
        hdrs.update(headers)

    reader = BodyReader(
        pause=lambda: None,
        resume=lambda: None,
        disconnect=None,
    )
    reader._cached = body
    reader._complete = True

    return Request(
        method=method,
        path=path,
        query_bytes=urlencode(query or {}, doseq=True).encode("ascii"),
        headers=hdrs,
        body=reader,
    )


@asynccontextmanager
async def bootstrap(app: Any, span: Any):
    yield


def make_server(*, graceful_timeout: float) -> Server:
    return Server(bootstrap, JsonTracer(StringIO()), graceful_timeout=graceful_timeout)


def make_context() -> Context:
    tracer = JsonTracer(StringIO())
    app = App()
    return Context(
        app=app,
        req=_make_request(),
        span=tracer.create("request"),
        state={},
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
        context = make_context()

        async def worker() -> int:
            await asyncio.sleep(0)
            return 42

        task = context.app.create_task(worker(), name="managed-worker")

        assert task in context.app._tasks
        assert task.get_name() == "managed-worker"
        assert await task == 42

        await asyncio.sleep(0)
        assert not context.app._tasks


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


def test_record_startup_attrs_includes_event_loop_name() -> None:
    server = Server(
        bootstrap,
        JsonTracer(StringIO()),
        graceful_timeout=5.0,
        event_loop_name="uvloop",
    )

    captured: dict[str, object] = {}

    class FakeSpan:
        def attrs(self, attributes: dict[str, object]) -> None:
            captured.update(attributes)

    server._record_startup_attrs(cast(Span, FakeSpan()))

    assert captured["server.event_loop"] == "uvloop"


def test_record_startup_attrs_include_unix_socket_mode() -> None:
    server = Server(
        bootstrap,
        JsonTracer(StringIO()),
        unix_socket="/tmp/stario.sock",
        unix_socket_mode=0o640,
    )

    captured: dict[str, object] = {}

    class FakeSpan:
        def attrs(self, attributes: dict[str, object]) -> None:
            captured.update(attributes)

    server._record_startup_attrs(cast(Span, FakeSpan()))

    assert captured["server.listen_mode"] == "unix_socket"
    assert captured["server.unix_socket_mode"] == "0o640"


def test_create_unix_socket_uses_hardened_default_mode() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "stario.sock")
        server = Server(
            bootstrap,
            JsonTracer(StringIO()),
            unix_socket=path,
        )

        sock = server._create_unix_socket()
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o660
        finally:
            sock.close()
            server._cleanup_unix_socket()
