"""Bootstrap loading for integration tests."""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from stario.http.app import App
from stario.http.bootstrap import Bootstrap, ShutdownTrigger, bootstrap_run
from stario.telemetry.spans import ProxySpan
from stario.testing.tracer import TestTracer


@asynccontextmanager
async def aload_app(
    bootstrap: Bootstrap,
    *,
    app_factory: Callable[[], App] | None = None,
    tracer: TestTracer | None = None,
) -> AsyncGenerator[App]:
    """Load `bootstrap` like production; emits `server.startup` / `server.shutdown` spans.

    Pass `tracer` to share one `TestTracer` with `TestClient` (same instance
    as `client.tracer` when the client wires bootstrap).
    """
    t = tracer if tracer is not None else TestTracer()
    t.__enter__()

    app = (app_factory or App)()
    span = ProxySpan(t.create("server.startup"))
    span.start()
    span.attr("test.aload_app", True)

    startup_ended = False
    shutdown_opened = False
    body_failed = False

    def begin_shutdown(trigger: ShutdownTrigger) -> None:
        nonlocal shutdown_opened
        if shutdown_opened:
            return
        startup_id = span.id
        span.replace(t.create("server.shutdown"))
        span.start()
        span.link("server.startup", startup_id)
        span.attr("server.shutdown.trigger", trigger)
        shutdown_opened = True

    try:
        async with bootstrap_run(bootstrap, app, span):
            span.end()
            startup_ended = True
            try:
                yield app
            except BaseException as exc:
                body_failed = True
                app.signal_shutdown()
                begin_shutdown("runtime_failure")
                span.exception(exc)
                span.fail(str(exc))
                raise
            else:
                app.signal_shutdown()
                begin_shutdown("expected_stop")
    except BaseException as exc:
        if not body_failed:
            span.exception(exc)
            span.fail(str(exc))
        raise
    finally:
        app.signal_shutdown()
        if startup_ended and not shutdown_opened:
            begin_shutdown("fallback_cleanup")
        span.end()
        t.__exit__(None, None, None)
