"""Worker-thread runtime: shutdown fan-out, loop runner forwarding, STARIO_THREADS."""

import asyncio
import json
import os
import queue
import tempfile
import threading
from io import StringIO
from typing import Any

import pytest

import stario.responses as responses
from stario import App
from stario.exceptions import StarioError
from stario.http.config import ServerConfig
from stario.http.server import Server, gil_is_enabled, require_thread_workers
from stario.telemetry.json import JsonTracer
from tests.test_server import _connect_with_retry, _read_http_response, _serve


@pytest.fixture
def short_socket_path():
    """AF_UNIX paths are limited to ~104 bytes on macOS; pytest's tmp_path is too long."""
    path = os.path.join(
        tempfile.gettempdir(), f"stario-thr-{os.getpid()}-{os.urandom(3).hex()}.sock"
    )
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_require_thread_workers_allows_one() -> None:
    require_thread_workers(1)


def test_require_thread_workers_rejects_gil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STARIO_THREADS_ALLOW_GIL", raising=False)
    if not gil_is_enabled():
        pytest.skip("GIL is already disabled")
    with pytest.raises(StarioError, match="STARIO_THREADS>1"):
        require_thread_workers(2)


def test_require_thread_workers_allow_gil_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    require_thread_workers(4)


def test_require_thread_workers_gil_reenabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STARIO_THREADS_ALLOW_GIL", raising=False)
    monkeypatch.setattr("stario.http.server.gil_is_enabled", lambda: True)
    monkeypatch.setattr(
        "stario.http.server.sysconfig.get_config_var",
        lambda name: 1 if name == "Py_GIL_DISABLED" else None,
    )
    with pytest.raises(StarioError, match="GIL to stay disabled"):
        require_thread_workers(2)


async def test_signal_shutdown_completes_other_loop() -> None:
    app = App()
    started = threading.Event()
    results: queue.Queue[object] = queue.Queue()

    def run_other() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def wait_shutdown() -> None:
            app.attach_loop()
            started.set()
            await app.shutdown

        try:
            loop.run_until_complete(wait_shutdown())
            results.put("ok")
        except BaseException as exc:
            results.put(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=run_other)
    thread.start()
    assert started.wait(timeout=2.0)
    app.signal_shutdown()
    thread.join(timeout=2.0)
    assert results.get(timeout=1.0) == "ok"
    assert app.shutting_down


def test_loop_runner_is_cached() -> None:
    async def bootstrap(app: App, span: Any):
        yield

    server = Server(
        bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(threads=1, event_loop="asyncio"),
    )
    runner = server.loop_runner()
    assert runner is asyncio.run
    assert server.loop_runner() is runner


def test_loop_runner_forwards_uvloop() -> None:
    uvloop = pytest.importorskip("uvloop")

    async def bootstrap(app: App, span: Any):
        yield

    server = Server(
        bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(threads=2, event_loop="uvloop"),
    )
    runner = server.loop_runner()
    assert runner is uvloop.run
    assert runner is not asyncio.run
    assert server.loop_runner() is runner


@pytest.mark.asyncio
async def test_threaded_serve_uses_configured_runner(
    monkeypatch: pytest.MonkeyPatch, short_socket_path: str
) -> None:
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    runs: list[str] = []
    real_asyncio_run = asyncio.run

    def tracking_run(coro: Any) -> Any:
        runs.append(threading.current_thread().name)
        return real_asyncio_run(coro)

    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            responses.text(w, "hello-threads")

        app.get("/", hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            unix_socket=short_socket_path,
            graceful_shutdown_timeout=0.5,
            threads=2,
            event_loop="asyncio",
        ),
    )
    server._loop_run = tracking_run

    run_task = asyncio.create_task(_serve(server))
    try:
        reader, writer = await _connect_with_retry(short_socket_path)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-threads"
        assert {name.startswith("stario-worker-") for name in runs} == {True}
        assert len(runs) == 2
    finally:
        apps[0].signal_shutdown()
        async with asyncio.timeout(5.0):
            await run_task


@pytest.mark.asyncio
async def test_threaded_serve_records_thread_count(
    monkeypatch: pytest.MonkeyPatch, short_socket_path: str
) -> None:
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    output = StringIO()
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            responses.text(w, "ok")

        app.get("/", hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(output),
        config=ServerConfig(
            unix_socket=short_socket_path,
            graceful_shutdown_timeout=0.5,
            threads=2,
        ),
    )

    run_task = asyncio.create_task(_serve(server))
    try:
        reader, writer = await _connect_with_retry(short_socket_path)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        await _read_http_response(reader)
        writer.close()
    finally:
        apps[0].signal_shutdown()
        async with asyncio.timeout(5.0):
            await run_task

    spans = [json.loads(line) for line in output.getvalue().splitlines()]
    startup = next(s for s in spans if s["name"] == "server.startup")
    assert startup["attributes"]["server.threads"] == 2
    assert startup["attributes"]["server.event_loop"] == "asyncio"


@pytest.mark.asyncio
async def test_threaded_workers_use_uvloop_runtime(
    monkeypatch: pytest.MonkeyPatch, short_socket_path: str
) -> None:
    uvloop = pytest.importorskip("uvloop")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    loop_modules: list[str] = []
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            loop_modules.append(type(asyncio.get_running_loop()).__module__)
            responses.text(w, "hello-uvloop")

        app.get("/", hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            unix_socket=short_socket_path,
            graceful_shutdown_timeout=0.5,
            threads=2,
            event_loop="uvloop",
        ),
    )
    assert server.loop_runner() is uvloop.run

    run_task = asyncio.create_task(_serve(server))
    try:
        reader, writer = await _connect_with_retry(short_socket_path)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-uvloop"
        assert loop_modules
        assert all(module.startswith("uvloop") for module in loop_modules)
    finally:
        apps[0].signal_shutdown()
        async with asyncio.timeout(5.0):
            await run_task
