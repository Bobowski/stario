"""
Request-scoped bundle for handlers: app, request, telemetry, routing, state, and client lifetime.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, overload

from stario.telemetry.core import Span

from .request import Request
from .writer import Writer

if TYPE_CHECKING:
    from .app import App


@dataclass(slots=True, frozen=True)
class RouteMatch:
    """Result of routing: a canonical pattern string plus captured path/host segments."""

    pattern: str
    """Matched route template (useful for logs), including host part when present."""
    params: Mapping[str, str]
    """Map from `{param}` / `{rest...}` names to decoded segment text."""


EMPTY_ROUTE_MATCH = RouteMatch(pattern="", params=MappingProxyType({}))


@dataclass(slots=True)
class Context:
    """Per-request bundle passed to every handler and middleware (routing fills `route` before the handler runs)."""

    app: App
    """The `App` instance for this request."""
    req: Request
    """Parsed HTTP request (method, path, headers, body reader)."""
    span: Span
    """Telemetry span for this request; started/ended by the app callable."""
    _disconnect: asyncio.Future[None] = field(repr=False)
    """Completes when the client closes this request's connection."""
    state: dict[str, Any] = field(default_factory=lambda: {})
    """Mutable dict for middleware to pass data to inner layers and the handler."""
    route: RouteMatch = field(default=EMPTY_ROUTE_MATCH)
    """Filled by `App.__call__` before the handler runs; do not assign in handlers."""

    @property
    def disconnect(self) -> asyncio.Future[None]:
        """Completes when the client closes this request's connection."""
        return self._disconnect

    @property
    def disconnected(self) -> bool:
        """`True` when the client closed this request's connection."""
        return self._disconnect.done()

    @property
    def shutting_down(self) -> bool:
        """`True` when the server is draining this app (same signal as `app.shutting_down`)."""
        return self.app.shutting_down

    @property
    def closing(self) -> bool:
        """`True` when handler work should stop because the client left or the app is draining.

        For response I/O during drain, `Writer` may still write until `disconnected`.
        """
        return self.disconnected or self.shutting_down

    @overload
    def alive(self, source: None = None) -> _Alive[None]: ...

    @overload
    def alive[T](self, source: AsyncIterable[T]) -> _Alive[T]: ...

    def alive[T](
        self,
        source: AsyncIterable[T] | None = None,
    ) -> _Alive[T] | _Alive[None]:
        """Watch client disconnect and app shutdown; cancel this task when either happens.

        Use `async with c.alive(): ...` for scoped work, or
        `async for item in c.alive(source): ...` to stream from `source` until
        disconnect or shutdown. Do not use `async for` without `source`; the
        context-manager form is the supported no-source pattern.
        """
        return _Alive(self, source)


@dataclass(slots=True)
class _Alive[T]:
    """Connection lifecycle helper bound to a request context."""

    c: Context
    source: AsyncIterable[T] | None = None
    watcher: asyncio.Task[None] | None = None
    cancelled_current_task: bool = False

    async def __aiter__(self) -> AsyncIterator[T]:
        if self.source is None:
            raise RuntimeError(
                "Use `async with c.alive():` when not streaming a source."
            )
        async with self:
            async for item in self.source:
                yield item

    async def __aenter__(self) -> _Alive[T]:
        current_task = asyncio.current_task()
        disconnect = self.c.disconnect
        shutdown = self.c.app.shutdown

        async def watcher() -> None:
            await asyncio.wait(
                {disconnect, shutdown},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if current_task and not current_task.done():
                self.cancelled_current_task = True
                current_task.cancel()

        self.watcher = asyncio.create_task(watcher(), name="stario.context.alive")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> bool:
        if self.watcher:
            self.watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watcher

        return (
            exc_type is not None
            and issubclass(exc_type, asyncio.CancelledError)
            and self.cancelled_current_task
        )


type Handler = Callable[[Context, Writer], Awaitable[None]]

type Middleware = Callable[[Handler], Handler]
