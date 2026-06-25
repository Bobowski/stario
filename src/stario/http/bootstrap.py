"""
`bootstrap(app, span)` contract for `Server` and tests.

Bootstrap must be an async generator that yields exactly once: code before `yield`
is startup, code after is shutdown. The framework advances it with `anext()` so
server failures never enter the generator.
"""

import inspect
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from stario.exceptions import StarioError
from stario.telemetry.core import Span

from .app import App

type Bootstrap = Callable[[App, Span], AsyncGenerator[None]]
type ShutdownTrigger = Literal[
    "expected_stop",
    "runtime_failure",
    "fallback_cleanup",
]


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
    if inspect.isasyncgen(result):
        gen = result
    elif inspect.iscoroutine(result):
        result.close()
        raise StarioError(
            "Bootstrap must yield exactly once",
            help_text="Define `async def bootstrap(app, span): ...; yield` (one yield).",
        )
    else:
        raise StarioError(
            "Bootstrap must be an async generator",
            context={"type": type(result).__name__},
            help_text="Define `async def bootstrap(app, span): ...; yield` (one yield).",
        )

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
