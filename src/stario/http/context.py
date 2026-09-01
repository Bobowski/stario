"""Request-scoped handler bundle: Protocol plus route match and ``alive()``.

Production ``c`` is the Cython ``RequestExchange``. TestClient supplies its own
context. ``RouteMatch`` and ``_Alive`` stay as small Python helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, overload

from stario.telemetry.core import Span

if TYPE_CHECKING:
    from stario.http.app import App
    from stario.http.request import Request
    from stario.http.writer import Writer


@dataclass(slots=True, frozen=True)
class RouteMatch:
    """Result of routing: a canonical pattern string plus captured path/host segments."""

    pattern: str
    """Matched route template (useful for logs), including host part when present."""
    params: Mapping[str, str]
    """Map from `{param}` / `{rest...}` names to decoded segment text."""


EMPTY_ROUTE_MATCH = RouteMatch(pattern="", params=MappingProxyType({}))


class Context(Protocol):
    """Per-request bundle: inbound request, app, span, and this request's lifetime.

    Every handler is ``async def handler(c: Context, w: Writer)``. ``c`` is
    everything about *this request still being live work*: what they asked
    (``req``, ``route``), the process (``app``), observability (``span``),
    request-scoped ``state``, and whether the handler should keep running.

    ``c.alive()`` / ``c.disconnected`` / ``c.closing`` live here — not on
    ``Writer`` — because they answer “should this handler still run?”, not
    “what HTTP bytes am I sending?”. That includes the client leaving *and*
    the app draining. Shutdown is already ``c.app``; the inbound body already
    surfaces peer-gone as ``ClientDisconnected`` on ``c.req``. ``Writer`` is
    the outbound message (``started`` / ``completed`` / ``write``). After
    ``w.end()`` the response is complete even if keep-alive is still open;
    ``w.write()`` is a no-op if the transport is already gone.
    """

    app: App
    req: Request
    span: Span
    state: dict[str, Any]
    route: RouteMatch

    @property
    def disconnect(self) -> asyncio.Future[None]:
        """Completes when the client is gone from this request."""
        ...

    @property
    def disconnected(self) -> bool:
        """``True`` when the client is gone from this request."""
        ...

    @property
    def shutting_down(self) -> bool:
        """``True`` when the server is draining this app."""
        ...

    @property
    def closing(self) -> bool:
        """``True`` when handler work should stop (client left or app draining)."""
        ...

    @overload
    def alive(self, source: None = None) -> _Alive[None]: ...

    @overload
    def alive[T](self, source: AsyncIterable[T]) -> _Alive[T]: ...

    def alive[T](
        self,
        source: AsyncIterable[T] | None = None,
    ) -> _Alive[T] | _Alive[None]:
        """Keep this handler running until the client leaves or the app drains.

        ``async with c.alive():`` cancels the current task when either happens,
        then swallows that cancellation so the block exits normally (code after
        the ``async with`` still runs). ``async for item in c.alive(source):``
        does the same around an async iterable.

        This is handler lifetime, not a write method: a long-lived loop can
        wait here without sending, and ``SSE(w)`` still only needs ``Writer``
        for the bytes.
        """
        ...


@dataclass(slots=True)
class _Alive[T]:
    """Handler-lifetime helper bound to a request context."""

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


type Handler = Callable[[Context, "Writer"], Awaitable[None]]
type Middleware = Callable[[Handler], Handler]
