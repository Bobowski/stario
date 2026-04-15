"""
Normalize app entrypoints for ``Server`` and tests: plain async functions, sync functions, async context managers,
single-yield async generators, and ``None`` become one ``AsyncContextManager`` contract.
"""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncContextManager, TypeGuard, cast

from stario.exceptions import StarioError
from stario.telemetry.core import Span

from .app import App
from .server import AppBootstrap

type BootstrapResult = (
    AsyncContextManager[object] | AsyncIterator[None] | Awaitable[object] | None
)
type BootstrapCandidate = Callable[[App, Span], BootstrapResult]


def _is_async_context_manager(
    value: object,
) -> TypeGuard[AsyncContextManager[object]]:
    return hasattr(value, "__aenter__") and hasattr(value, "__aexit__")


@asynccontextmanager
async def _bootstrap_async_generator_scope(
    generator: AsyncIterator[None],
) -> AsyncIterator[None]:
    try:
        await anext(generator)
    except StopAsyncIteration as exc:
        raise StarioError(
            "Bootstrap async generator did not yield",
            help_text="Add a single `yield` for teardown support or return normally for setup-only bootstraps.",
        ) from exc

    try:
        yield
    finally:
        try:
            await anext(generator)
        except StopAsyncIteration:
            pass
        else:
            raise StarioError(
                "Bootstrap async generator yielded more than once",
                help_text="Bootstrap async generators must yield exactly once.",
            )


def normalize_bootstrap(bootstrap: BootstrapCandidate) -> AppBootstrap:
    """Wrap a user ``bootstrap(app, span)`` so it always acts as an async context manager.

    Parameters:
        bootstrap: Callable returning an async context manager, a single-yield async generator, an awaitable, or ``None``.

    Returns:
        ``AppBootstrap`` — use as ``async with wrapped(app, span):`` from ``Server`` or tests.

    Raises:
        StarioError: From the wrapper when the return type is unsupported or an async generator mis-``yield``s.

    Notes:
        Async generators must ``yield`` exactly once between setup and teardown.
    """
    @asynccontextmanager
    async def wrapped(app: App, span: Span) -> AsyncIterator[None]:
        result = bootstrap(app, span)
        if _is_async_context_manager(result):
            async with result:
                yield
            return

        if inspect.isasyncgen(result):
            async with _bootstrap_async_generator_scope(result):
                yield
            return

        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)
            yield
            return

        if result is None:
            yield
            return

        raise StarioError(
            "Unsupported bootstrap return type",
            context={"type": type(result).__name__},
            help_text="Return an async context manager, an async generator, an awaitable, or None.",
        )

    return wrapped
