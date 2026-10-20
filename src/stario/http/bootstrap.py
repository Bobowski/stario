"""
`bootstrap(app, span)` contract for `Server` and tests.

Bootstrap must be an async generator that yields exactly once: code before `yield`
is startup, code after is shutdown. The framework advances it with `anext()` so
server failures never enter the generator.
"""

import inspect
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Literal, cast

from stario.exceptions import StarioError
from stario.telemetry.core import Span, Tracer
from stario.telemetry.spans import ProxySpan

from .app import App

type Bootstrap = Callable[[App, Span], AsyncGenerator[None]]
type ShutdownTrigger = Literal[
    "expected_stop",
    "runtime_failure",
    "fallback_cleanup",
]


class AppRun:
    """One running app: the `App` plus its startup → shutdown `ProxySpan`."""

    __slots__ = ("_shutdown_opened", "_startup_ended", "_tracer", "app", "span")

    def __init__(self, app: App, span: ProxySpan, tracer: Tracer) -> None:
        self.app = app
        self.span = span
        self._tracer = tracer
        self._startup_ended = False
        self._shutdown_opened = False

    @property
    def startup_ended(self) -> bool:
        return self._startup_ended

    @property
    def shutdown_opened(self) -> bool:
        return self._shutdown_opened

    def end_startup(self) -> None:
        """End the startup span once. No-op after shutdown began."""
        if self._startup_ended or self._shutdown_opened:
            return
        self._startup_ended = True
        self.span.end()

    def begin_shutdown(self, trigger: ShutdownTrigger) -> None:
        """Swap the span to `server.shutdown` (once) and signal the app."""
        self.app.signal_shutdown()
        if self._shutdown_opened:
            return
        self.end_startup()
        startup_id = self.span.id
        shutdown = self._tracer.create("server.shutdown")
        shutdown.link("server.startup", startup_id)
        shutdown.attr("server.shutdown.trigger", trigger)
        shutdown.start()
        self.span.replace(shutdown)
        self._shutdown_opened = True


@asynccontextmanager
async def run_app(
    bootstrap: Bootstrap,
    tracer: Tracer,
    *,
    app_factory: Callable[[], App] | None = None,
) -> AsyncGenerator[AppRun]:
    """Run `bootstrap` on a fresh `App` with the production span lifecycle.

    Entering runs bootstrap startup (until its `yield`); leaving runs the
    teardown. The startup span stays open for the caller (`end_startup()`);
    `begin_shutdown()` swaps it for `server.shutdown` exactly once.
    """
    app = (app_factory or App)()
    span = ProxySpan(tracer.create("server.startup"))
    span.start()
    run = AppRun(app, span, tracer)

    body_failed = False
    try:
        async with bootstrap_run(bootstrap, app, span):
            app.compiled()
            app.freeze()
            try:
                yield run
            except BaseException as exc:
                body_failed = True
                run.begin_shutdown("runtime_failure")
                span.exception(exc)
                span.fail(str(exc))
                raise
            else:
                run.begin_shutdown("expected_stop")
    except BaseException as exc:
        if not body_failed:
            span.exception(exc)
            span.fail(str(exc))
        raise
    finally:
        app.signal_shutdown()
        if run.startup_ended and not run.shutdown_opened:
            run.begin_shutdown("fallback_cleanup")
        span.end()


@asynccontextmanager
async def bootstrap_run(
    bootstrap: Bootstrap,
    app: App,
    span: Span,
) -> AsyncGenerator[None]:
    """Bootstrap startup on enter, teardown on exit.

    Uses `anext()` on the user generator — not `async with` on it — so server
    failures in the scoped body never enter bootstrap teardown via `.athrow()`.
    """
    result = bootstrap(app, span)
    if inspect.iscoroutine(result):
        result.close()
        raise StarioError(
            "Bootstrap must yield exactly once",
            help_text="Define `async def bootstrap(app, span): ...; yield` (one yield).",
        )
    if not inspect.isasyncgen(result):
        raise StarioError(
            "Bootstrap must be an async generator",
            context={"type": type(result).__name__},
            help_text="Define `async def bootstrap(app, span): ...; yield` (one yield).",
        )
    gen = cast(AsyncGenerator[None], result)

    try:
        await anext(gen)
    except StopAsyncIteration as exc:
        await gen.aclose()
        raise StarioError(
            "Bootstrap must yield exactly once",
            help_text="Add `yield` after startup wiring and before teardown code.",
        ) from exc
    except Exception:
        await gen.aclose()
        raise

    try:
        yield
    finally:
        try:
            await anext(gen)
        except StopAsyncIteration:
            pass
        else:
            await gen.aclose()
            raise StarioError(
                "Bootstrap must not yield more than once",
                help_text="Bootstrap async generators must yield exactly once.",
            )
