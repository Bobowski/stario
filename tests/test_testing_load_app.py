"""``aload_app`` plus ``TestClient`` wire production bootstrap into tests."""

from contextlib import asynccontextmanager

import pytest

import stario.responses as responses
import stario.testing as stio_testing
from stario import App
from stario.telemetry.core import Span
from stario.testing import TestClient, aload_app


async def _minimal_bootstrap(app: App, span) -> None:
    async def ping(c, w):
        responses.text(w, "pong")

    app.get("/ping", ping)


@pytest.mark.asyncio
async def test_aload_app_registers_routes() -> None:
    async with aload_app(_minimal_bootstrap) as app:
        async with TestClient(app) as client:
            assert (await client.get("/ping")).text == "pong"


@pytest.mark.asyncio
async def test_test_client_accepts_bootstrap_directly() -> None:
    async with TestClient(_minimal_bootstrap) as client:
        assert (await client.get("/ping")).text == "pong"
        assert isinstance(client.app, App)


@pytest.mark.asyncio
async def test_test_client_bootstrap_requires_context() -> None:
    client = TestClient(_minimal_bootstrap)
    with pytest.raises(RuntimeError, match="async with TestClient"):
        await client.get("/ping")


@pytest.mark.asyncio
async def test_aload_app_runs_teardown_for_async_context_bootstrap() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def bootstrap(app: App, span):
        events.append("in")
        yield
        events.append("out")

    async with aload_app(bootstrap):
        assert events == ["in"]
    assert events == ["in", "out"]


@pytest.mark.asyncio
async def test_aload_app_signals_app_shutdown_before_bootstrap_teardown() -> None:
    shutdown_seen = False

    @asynccontextmanager
    async def bootstrap(app: App, span):
        nonlocal shutdown_seen
        yield
        shutdown_seen = app.shutting_down

    async with aload_app(bootstrap):
        pass

    assert shutdown_seen


@pytest.mark.asyncio
async def test_test_client_inside_aload_app_does_not_own_app_shutdown() -> None:
    shutdown_seen = False

    @asynccontextmanager
    async def bootstrap(app: App, span):
        nonlocal shutdown_seen

        async def ping(c, w):
            responses.text(w, "pong")

        app.get("/ping", ping)
        yield
        shutdown_seen = app.shutting_down

    async with aload_app(bootstrap) as app:
        async with TestClient(app) as client:
            assert (await client.get("/ping")).text == "pong"
        assert not app.shutting_down

    assert shutdown_seen


@pytest.mark.asyncio
async def test_aload_app_emits_server_startup_and_shutdown_like_cli() -> None:
    """Match ``Server`` startup/shutdown: startup span ends after the yielded body; shutdown is separate."""

    startup_phase: list[str] = []
    shutdown_phase: list[str] = []

    async def bootstrap(app: App, span: Span):
        span.attr("phase.boot", "setup")
        startup_phase.append(span.id.hex)
        yield
        span.attr("phase.boot", "teardown")
        shutdown_phase.append(span.id.hex)

    app = App()

    async def ping(c, w):
        responses.text(w, "pong")

    app.get("/ping", ping)

    tracer = stio_testing.TestTracer()
    async with aload_app(bootstrap, app_factory=lambda: app, tracer=tracer):
        pass

    startup = tracer.find_span("server.startup")
    shutdown = tracer.find_span("server.shutdown")
    assert startup is not None
    assert shutdown is not None
    assert startup.attributes["phase.boot"] == "setup"
    assert shutdown.attributes["phase.boot"] == "teardown"
    assert shutdown.attributes["server.shutdown.trigger"] == "expected_stop"
    assert startup_phase == [startup.id.hex]
    assert shutdown_phase == [shutdown.id.hex]
    assert any(
        ln.span_id == startup.id for ln in shutdown.links
    )


@pytest.mark.asyncio
async def test_aload_app_custom_factory() -> None:
    created: list[App] = []

    def factory() -> App:
        app = App()
        created.append(app)
        return app

    async with aload_app(_minimal_bootstrap, app_factory=factory) as app:
        async with TestClient(app) as client:
            assert (await client.get("/ping")).status_code == 200
    assert created == [app]
