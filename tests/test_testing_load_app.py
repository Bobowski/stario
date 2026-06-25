"""`aload_app` plus `TestClient` wire production bootstrap into tests."""

import pytest

import stario.responses as responses
import stario.testing as stio_testing
from stario import App
from stario.telemetry.core import Span
from stario.testing import TestClient, aload_app


async def _minimal_bootstrap(app: App, span):
    async def ping(c, w):
        responses.text(w, "pong")

    app.get("/ping", ping)
    yield


@pytest.mark.asyncio
async def test_aload_app_registers_routes() -> None:
    async with aload_app(_minimal_bootstrap) as app, TestClient(app) as client:
        assert (await client.get("/ping")).text == "pong"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bootstrap", "app"])
async def test_test_client_requires_context(kind: str) -> None:
    client = (
        TestClient(_minimal_bootstrap) if kind == "bootstrap" else TestClient(App())
    )
    with pytest.raises(RuntimeError, match="async with TestClient"):
        await client.get("/ping")


@pytest.mark.asyncio
async def test_aload_app_runs_teardown_for_bootstrap_generator() -> None:
    events: list[str] = []

    async def bootstrap(app: App, span):
        events.append("in")
        yield
        events.append("out")

    async with aload_app(bootstrap):
        assert events == ["in"]
    assert events == ["in", "out"]


@pytest.mark.asyncio
async def test_test_client_inside_aload_app_does_not_own_app_shutdown() -> None:
    shutdown_seen = False

    async def bootstrap(app: App, span):
        nonlocal shutdown_seen

        async def ping(c, w):
            responses.text(w, "pong")

        app.get("/ping", ping)
        yield
        shutdown_seen = app.shutting_down

    async with aload_app(bootstrap) as app:
        async with TestClient(app, owns_shutdown=False) as client:
            assert (await client.get("/ping")).text == "pong"
        assert not app.shutting_down

    assert shutdown_seen


@pytest.mark.asyncio
async def test_aload_app_emits_server_startup_and_shutdown_like_cli() -> None:
    """Match `Server` startup/shutdown: startup span ends after the yielded body; shutdown is separate."""

    async def bootstrap(app: App, span: Span):
        span.attr("phase.boot", "setup")
        yield
        span.attr("phase.boot", "teardown")

    async def ping(c, w):
        responses.text(w, "pong")

    tracer = stio_testing.TestTracer()

    def provide_app() -> App:
        app = App()
        app.get("/ping", ping)
        return app

    async with aload_app(bootstrap, app_factory=provide_app, tracer=tracer):
        pass

    startup = tracer.find_span("server.startup")
    shutdown = tracer.find_span("server.shutdown")
    assert startup is not None
    assert shutdown is not None
    assert startup.attributes["phase.boot"] == "setup"
    assert shutdown.attributes["phase.boot"] == "teardown"
    assert shutdown.attributes["server.shutdown.trigger"] == "expected_stop"


@pytest.mark.asyncio
async def test_aload_app_startup_failure_marks_startup_span_failed() -> None:
    tracer = stio_testing.TestTracer()

    async def bootstrap(app: App, span):
        raise RuntimeError("boot failed")
        yield

    with pytest.raises(RuntimeError, match="boot failed"):
        async with aload_app(bootstrap, tracer=tracer):
            pass

    startup = tracer.find_span("server.startup")
    assert startup is not None
    assert startup.error == "boot failed"
    assert any(ev.name == "exception" for ev in startup.events)
    # Startup never completed, so no shutdown span exists.
    assert tracer.find_span("server.shutdown") is None


@pytest.mark.asyncio
async def test_aload_app_teardown_failure_marks_shutdown_span_failed() -> None:
    tracer = stio_testing.TestTracer()

    async def bootstrap(app: App, span):
        yield
        raise RuntimeError("teardown failed")

    with pytest.raises(RuntimeError, match="teardown failed"):
        async with aload_app(bootstrap, tracer=tracer):
            pass

    shutdown = tracer.find_span("server.shutdown")
    assert shutdown is not None
    assert shutdown.attributes.get("server.shutdown.trigger") == "expected_stop"
    assert shutdown.error == "teardown failed"


@pytest.mark.asyncio
async def test_aload_app_signals_drain_on_exit() -> None:
    async with aload_app(_minimal_bootstrap) as app:
        assert not app.shutting_down
    assert app.shutting_down


@pytest.mark.asyncio
async def test_aload_app_body_failure_emits_runtime_failure_shutdown() -> None:
    tracer = stio_testing.TestTracer()

    async def bootstrap(app: App, span):
        yield

    with pytest.raises(RuntimeError, match="body failed"):
        async with aload_app(bootstrap, tracer=tracer):
            raise RuntimeError("body failed")

    shutdown = tracer.find_span("server.shutdown")
    assert shutdown is not None
    assert shutdown.attributes.get("server.shutdown.trigger") == "runtime_failure"
    assert shutdown.error is not None
