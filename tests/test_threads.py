"""Worker-thread runtime: shutdown fan-out, loop runner forwarding, STARIO_THREADS."""

import asyncio
import gc
import json
import os
import queue
import socket
import sys
import tempfile
import threading
from io import StringIO
from typing import Any

import pytest

import stario.responses as responses
from stario import App, Route
from stario.exceptions import StarioError
from stario.http.config import ServerConfig
from stario.http.server import (
    Server,
    gil_is_enabled,
    loop_implementation,
    require_configured_loop,
    require_thread_workers,
    resolve_loop_runner,
    reuseport_supported,
)
from stario.telemetry.json import JsonTracer
from tests.test_server import _connect_with_retry, _read_http_response, _serve


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _connect_tcp(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    async with asyncio.timeout(2.0):
        while True:
            try:
                return await asyncio.open_connection(host, port)
            except OSError:
                await asyncio.sleep(0.005)


def _run_server(server: Server) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def body() -> None:
        try:
            with server.tracer:
                server.run()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=body, name="stario-main")
    thread.start()
    return thread, errors


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
    assert runner is resolve_loop_runner("asyncio")
    assert runner is not asyncio.run
    assert server.loop_runner() is runner


def test_loop_implementation_classifies_running_loop() -> None:
    seen: list[str] = []

    async def probe() -> None:
        seen.append(loop_implementation())

    asyncio.run(probe(), loop_factory=asyncio.SelectorEventLoop)
    assert seen == ["asyncio"]


def test_asyncio_runner_ignores_uvloop_policy() -> None:
    uvloop = pytest.importorskip("uvloop")
    previous = asyncio.get_event_loop_policy()
    uvloop.install()
    try:
        seen: list[str] = []

        async def probe() -> None:
            seen.append(loop_implementation())
            seen.append(type(asyncio.get_running_loop()).__module__)

        resolve_loop_runner("asyncio")(probe())
        assert seen[0] == "asyncio"
        assert not seen[1].startswith("uvloop")
        # The process policy still prefers uvloop; only our runner is pinned.
        policy_loop = asyncio.new_event_loop()
        try:
            assert loop_implementation(policy_loop) == "uvloop"
        finally:
            policy_loop.close()
    finally:
        asyncio.set_event_loop_policy(previous)


def test_uvloop_runner_creates_uvloop() -> None:
    pytest.importorskip("uvloop")
    seen: list[str] = []

    async def probe() -> None:
        seen.append(loop_implementation())

    resolve_loop_runner("uvloop")(probe())
    assert seen == ["uvloop"]


def test_require_configured_loop_rejects_mismatch() -> None:
    async def probe() -> None:
        require_configured_loop("uvloop", where="test")

    with pytest.raises(StarioError, match="running asyncio, configured uvloop"):
        resolve_loop_runner("asyncio")(probe())


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
    assert runner is not resolve_loop_runner("asyncio")
    assert server.loop_runner() is runner


def test_reuseport_supported_on_tcp() -> None:
    if sys.platform == "win32":
        pytest.skip("SO_REUSEPORT is not available on Windows")
    assert reuseport_supported(unix=False) is True


def test_unix_reuseport_probe_does_not_raise() -> None:
    supported = reuseport_supported(unix=True)
    assert supported in (True, False)


@pytest.mark.asyncio
async def test_unix_threads_fall_back_to_one(
    monkeypatch: pytest.MonkeyPatch, short_socket_path: str
) -> None:
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    monkeypatch.setattr(
        "stario.http.server.reuseport_supported", lambda **_kwargs: False
    )
    output = StringIO()
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            responses.text(w, "one-thread")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(output),
        config=ServerConfig(
            unix_socket=short_socket_path,
            graceful_shutdown_timeout=0.5,
            threads=4,
        ),
    )

    run_task = asyncio.create_task(_serve(server))
    try:
        reader, writer = await _connect_with_retry(short_socket_path)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"one-thread"
    finally:
        apps[0].signal_shutdown()
        async with asyncio.timeout(5.0):
            await run_task

    spans = [json.loads(line) for line in output.getvalue().splitlines()]
    startup = next(s for s in spans if s["name"] == "server.startup")
    assert startup["attributes"]["server.threads"] == 1
    assert startup["attributes"]["server.threads_requested"] == 4
    assert startup["attributes"]["server.listen_balance"] == "single"


@pytest.mark.asyncio
async def test_threaded_serve_uses_configured_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    runs: list[str] = []
    kinds: list[str] = []
    modules: list[str] = []
    real_run = resolve_loop_runner("asyncio")

    def tracking_run(coro: Any) -> Any:
        runs.append(threading.current_thread().name)
        return real_run(coro)

    apps: list[App] = []
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            kinds.append(loop_implementation())
            modules.append(type(asyncio.get_running_loop()).__module__)
            responses.text(w, "hello-threads")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
            event_loop="asyncio",
        ),
    )
    server._loop_run = tracking_run

    thread, errors = _run_server(server)
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-threads"
        assert "stario-main" in runs
        assert any(name.startswith("stario-worker-") for name in runs)
        assert len(runs) == 2
        assert kinds == ["asyncio"]
        assert modules
        assert all(not module.startswith("uvloop") for module in modules)
    finally:
        if apps:
            apps[0].signal_shutdown()
        thread.join(5.0)
        assert errors == []


@pytest.mark.asyncio
async def test_threaded_serve_records_thread_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    output = StringIO()
    apps: list[App] = []
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            responses.text(w, "ok")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(output),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
        ),
    )

    thread, errors = _run_server(server)
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        await _read_http_response(reader)
        writer.close()
    finally:
        if apps:
            apps[0].signal_shutdown()
        thread.join(5.0)
        assert errors == []

    spans = [json.loads(line) for line in output.getvalue().splitlines()]
    startup = next(s for s in spans if s["name"] == "server.startup")
    assert startup["attributes"]["server.threads"] == 2
    assert startup["attributes"]["server.threads_requested"] == 2
    assert startup["attributes"]["server.event_loop"] == "asyncio"
    assert startup["attributes"]["server.worker_event_loop"] == "asyncio"
    assert startup["attributes"]["server.listen_balance"] == "reuseport"


@pytest.mark.asyncio
async def test_threaded_workers_use_uvloop_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uvloop = pytest.importorskip("uvloop")
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    loop_modules: list[str] = []
    kinds: list[str] = []
    apps: list[App] = []
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            loop_modules.append(type(asyncio.get_running_loop()).__module__)
            kinds.append(loop_implementation())
            responses.text(w, "hello-uvloop")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
            event_loop="uvloop",
        ),
    )
    assert server.loop_runner() is uvloop.run

    thread, errors = _run_server(server)
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-uvloop"
        assert loop_modules
        assert all(module.startswith("uvloop") for module in loop_modules)
        assert kinds == ["uvloop"]
    finally:
        if apps:
            apps[0].signal_shutdown()
        thread.join(5.0)
        assert errors == []


@pytest.mark.asyncio
async def test_threaded_serve_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    port = _free_tcp_port()
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(_c: Any, w: Any) -> None:
            responses.text(w, "hello-tcp")

        app.add(Route("GET", "/"), hello)
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
            event_loop="asyncio",
        ),
    )

    run_task = asyncio.create_task(_serve(server))
    try:
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-tcp"
        gc.collect()
        reader, writer = await _connect_tcp("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        status, body = await _read_http_response(reader)
        writer.close()
        assert status == 200
        assert body == b"hello-tcp"
    finally:
        apps[0].signal_shutdown()
        async with asyncio.timeout(5.0):
            await run_task


@pytest.mark.asyncio
async def test_threaded_worker_rejects_loop_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
            event_loop="uvloop",
        ),
    )
    server._loop_run = resolve_loop_runner("asyncio")

    with pytest.raises(StarioError, match="running asyncio, configured uvloop"):
        await _serve(server)


@pytest.mark.asyncio
async def test_threaded_worker_rejects_uvloop_when_configured_asyncio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uvloop = pytest.importorskip("uvloop")
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    port = _free_tcp_port()

    async def serve_bootstrap(app: App, span: Any):
        yield

    server = Server(
        serve_bootstrap,
        JsonTracer(StringIO()),
        config=ServerConfig(
            host="127.0.0.1",
            port=port,
            graceful_shutdown_timeout=0.5,
            threads=2,
            event_loop="asyncio",
        ),
    )
    server._loop_run = uvloop.run

    with pytest.raises(StarioError, match="running uvloop, configured asyncio"):
        await _serve(server)


@pytest.mark.asyncio
async def test_threaded_asyncio_survives_uvloop_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STARIO_LOOP=asyncio must stay stdlib even if uvloop.install() ran."""
    uvloop = pytest.importorskip("uvloop")
    if not reuseport_supported(unix=False):
        pytest.skip("SO_REUSEPORT is required for STARIO_THREADS>1")
    monkeypatch.setenv("STARIO_THREADS_ALLOW_GIL", "1")
    previous = asyncio.get_event_loop_policy()
    uvloop.install()
    try:
        kinds: list[str] = []
        modules: list[str] = []
        apps: list[App] = []
        port = _free_tcp_port()

        async def serve_bootstrap(app: App, span: Any):
            apps.append(app)

            async def hello(_c: Any, w: Any) -> None:
                kinds.append(loop_implementation())
                modules.append(type(asyncio.get_running_loop()).__module__)
                responses.text(w, "still-asyncio")

            app.add(Route("GET", "/"), hello)
            yield

        server = Server(
            serve_bootstrap,
            JsonTracer(StringIO()),
            config=ServerConfig(
                host="127.0.0.1",
                port=port,
                graceful_shutdown_timeout=0.5,
                threads=2,
                event_loop="asyncio",
            ),
        )

        thread, errors = _run_server(server)
        try:
            reader, writer = await _connect_tcp("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            status, body = await _read_http_response(reader)
            writer.close()
            assert status == 200
            assert body == b"still-asyncio"
            assert kinds == ["asyncio"]
            assert modules
            assert all(not module.startswith("uvloop") for module in modules)
        finally:
            if apps:
                apps[0].signal_shutdown()
            thread.join(5.0)
            assert errors == []
    finally:
        asyncio.set_event_loop_policy(previous)


def test_run_rejects_loop_mismatch() -> None:
    async def bootstrap(app: App, span: Any):
        yield

    tracer = JsonTracer(StringIO())
    server = Server(
        bootstrap,
        tracer,
        config=ServerConfig(event_loop="uvloop", threads=1),
    )
    server._loop_run = resolve_loop_runner("asyncio")

    with (
        tracer,
        pytest.raises(
            StarioError, match=r"Server\.run is running asyncio, configured uvloop"
        ),
    ):
        server.run()


def test_run_uses_configured_asyncio_loop(short_socket_path: str) -> None:
    seen: list[str] = []

    async def bootstrap(app: App, span: Any):
        seen.append(loop_implementation())
        seen.append(type(asyncio.get_running_loop()).__module__)
        app.signal_shutdown()
        yield

    tracer = JsonTracer(StringIO())
    with tracer:
        Server(
            bootstrap,
            tracer,
            config=ServerConfig(
                unix_socket=short_socket_path,
                graceful_shutdown_timeout=0.5,
                event_loop="asyncio",
            ),
        ).run()
    assert seen[0] == "asyncio"
    assert not seen[1].startswith("uvloop")


def test_run_uses_configured_uvloop(short_socket_path: str) -> None:
    pytest.importorskip("uvloop")
    seen: list[str] = []

    async def bootstrap(app: App, span: Any):
        seen.append(loop_implementation())
        app.signal_shutdown()
        yield

    tracer = JsonTracer(StringIO())
    with tracer:
        Server(
            bootstrap,
            tracer,
            config=ServerConfig(
                unix_socket=short_socket_path,
                graceful_shutdown_timeout=0.5,
                event_loop="uvloop",
            ),
        ).run()
    assert seen == ["uvloop"]
