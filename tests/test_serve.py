"""Tests for `stario.serve` and `Server` construction defaults."""

import asyncio
import os
import tempfile
from io import StringIO
from typing import Any

import pytest

import stario.responses as responses
from stario import App, Route
from stario.cli.errors import CliError
from stario.cli.runtime import serve_once
from stario.exceptions import StarioError
from stario.http.config import ServerConfig
from stario.http.server import Server, serve
from stario.telemetry.json import JsonTracer
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.tty import TTYTracer


async def bootstrap(app: Any, span: Any):
    yield


def test_server_uses_config_defaults() -> None:
    server = Server(bootstrap, NoOpTracer())
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8000
    assert server.config.event_loop == "asyncio"
    assert server.config.unix_socket is None


async def test_serve_listen_fields_build_server_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ServerConfig] = []

    class FakeServer:
        def __init__(
            self, bootstrap: object, tracer: object, *, config: ServerConfig
        ) -> None:
            captured.append(config)

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap, port=8123, host="0.0.0.0")
    assert captured[0].port == 8123
    assert captured[0].host == "0.0.0.0"


async def test_serve_accepts_prepared_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[ServerConfig] = []
    cfg = ServerConfig(port=9001)

    class FakeServer:
        def __init__(
            self, bootstrap: object, tracer: object, *, config: ServerConfig
        ) -> None:
            captured.append(config)

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap, config=cfg)
    assert captured[0] is cfg


async def test_serve_rejects_config_and_listen_fields() -> None:
    with pytest.raises(StarioError, match="config= or listen fields"):
        await serve(bootstrap, config=ServerConfig(), port=9000)


def test_server_config_does_not_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARIO_PORT", "9000")
    monkeypatch.setenv("STARIO_HOST", "0.0.0.0")

    server = Server(bootstrap, NoOpTracer(), config=ServerConfig(port=8123))
    assert server.config.port == 8123
    assert server.config.host == "127.0.0.1"


async def test_serve_creates_default_tty_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class FakeServer:
        def __init__(self, bootstrap: object, tracer: object, **kwargs: object) -> None:
            captured.append(tracer)

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap)
    assert isinstance(captured[0], TTYTracer)


async def test_serve_enters_and_exits_default_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Probe:
        def __enter__(self) -> Probe:
            events.append("enter")
            return self

        def __exit__(self, *args: object) -> None:
            events.append("exit")

        def create(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("create should not run in this test")

        def on_end(self, span: object) -> None:
            return None

        def stats(self) -> None:
            return None

    class FakeServer:
        def __init__(self, bootstrap: object, tracer: object, **kwargs: object) -> None:
            assert events == ["enter"]

        async def serve(self) -> None:
            assert events == ["enter"]

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("stario.telemetry.tty.TTYTracer", Probe)
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap)
    assert events == ["enter", "exit"]


async def test_serve_creates_default_json_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class FakeServer:
        def __init__(self, bootstrap: object, tracer: object, **kwargs: object) -> None:
            captured.append(tracer)

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("stario.http.server.Server", FakeServer)
    await serve(bootstrap)
    assert isinstance(captured[0], JsonTracer)


def test_serve_once_constructs_server_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Server] = []

    class RecordingServer(Server):
        def run(self) -> None:
            captured.append(self)

    tracer = NoOpTracer()
    monkeypatch.setenv("STARIO_PORT", "9000")
    monkeypatch.setenv("STARIO_HOST", "0.0.0.0")
    monkeypatch.setattr("stario.cli.runtime.load_bootstrap", lambda spec: bootstrap)
    monkeypatch.setattr("stario.cli.runtime.tracer_from_env", lambda: tracer)
    monkeypatch.setattr("stario.cli.runtime.Server", RecordingServer)

    serve_once("demo:bootstrap")

    server = captured[0]
    assert server.bootstrap is bootstrap
    assert server.tracer is tracer
    assert server.config.port == 9000
    assert server.config.host == "0.0.0.0"


def test_serve_once_wraps_stario_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Server) -> None:
        raise StarioError("bind failed")

    monkeypatch.setattr("stario.cli.runtime.load_bootstrap", lambda spec: bootstrap)
    monkeypatch.setattr("stario.cli.runtime.tracer_from_env", lambda: NoOpTracer())
    monkeypatch.setattr("stario.cli.runtime.Server.run", boom)

    with pytest.raises(CliError, match="bind failed"):
        serve_once("demo:bootstrap")


@pytest.fixture
def short_socket_path():
    path = os.path.join(
        tempfile.gettempdir(), f"stario-serve-{os.getpid()}-{os.urandom(3).hex()}.sock"
    )
    yield path
    if os.path.exists(path):
        os.unlink(path)


async def test_serve_then_caller_continues(short_socket_path: str) -> None:
    apps: list[App] = []

    async def serve_bootstrap(app: App, span: Any):
        apps.append(app)

        async def hello(c: Any, w: Any) -> None:
            responses.text(w, "hello")

        app.add(Route("GET", "/"), hello)
        yield

    tracer = JsonTracer(StringIO())
    with tracer:
        task = asyncio.create_task(
            serve(
                serve_bootstrap,
                tracer,
                unix_socket=short_socket_path,
                graceful_shutdown_timeout=0.5,
            )
        )
        try:
            reader, writer = await _connect_with_retry(short_socket_path)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            body = b""
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    body = await reader.readexactly(int(line.split(b":", 1)[1]))
            writer.close()

            assert status == 200
            assert body == b"hello"
        finally:
            drain = apps[0].shutdown
            if not drain.done():
                drain.set_result(None)
            async with asyncio.timeout(2.0):
                await task

        # Caller still owns the tracer after the server stops.
        span = tracer.create("after.serve")
        span.start()
        span.end()

    assert not os.path.exists(short_socket_path)


async def _connect_with_retry(
    path: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    async with asyncio.timeout(2.0):
        while True:
            try:
                return await asyncio.open_unix_connection(path)
            except ConnectionRefusedError, FileNotFoundError:
                await asyncio.sleep(0.005)


def test_package_root_exports_serve() -> None:
    import stario

    assert stario.serve is serve
    assert "serve" in stario.__all__
