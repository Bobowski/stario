"""Request-scoped handler bundle: Protocol plus route match and ``alive()``.

Production ``c`` is the Cython ``RequestExchange``. TestClient supplies its own
context. ``Match`` and ``_Alive`` stay as small Python helpers.
"""

from __future__ import annotations

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, overload

from stario.telemetry.core import Span

_NO_PARAMS: MappingProxyType[str, str] = MappingProxyType({})


class Match:
    """Immutable hit: pattern plus this request's captures."""

    __slots__ = ("params", "pattern")

    def __init__(
        self,
        pattern: str,
        params: Mapping[str, str] = _NO_PARAMS,
    ) -> None:
        object.__setattr__(self, "pattern", pattern)
        if not params:
            frozen: Mapping[str, str] = _NO_PARAMS
        elif isinstance(params, MappingProxyType):
            frozen = params
        else:
            frozen = MappingProxyType(params)
        object.__setattr__(self, "params", frozen)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Match is immutable")

    def __repr__(self) -> str:
        if not self.pattern:
            return "Match.empty"
        return f"Match({self.pattern!r})"

    @classmethod
    def empty(cls) -> Match:
        """Unmatched 404 / 405 sentinel."""
        return EMPTY_MATCH


EMPTY_MATCH = Match.__new__(Match)
object.__setattr__(EMPTY_MATCH, "pattern", "")
object.__setattr__(EMPTY_MATCH, "params", _NO_PARAMS)

if TYPE_CHECKING:
    from stario.http.app import App
    from stario.http.request import Request
    from stario.http.writer import Writer


class Context(Protocol):
    """Per-request bundle passed to every handler and middleware."""

    app: App
    req: Request
    span: Span
    state: dict[str, Any]
    match: Match

    @property
    def disconnect(self) -> asyncio.Future[None]:
        """Completes when the client closes this request's connection."""
        ...

    @property
    def disconnected(self) -> bool:
        """``True`` when the client closed this request's connection."""
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
        """Watch client disconnect and app shutdown; cancel this task when either happens."""
        ...


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


type Handler = Callable[[Context, "Writer"], Awaitable[None]]
type Middleware = Callable[[Handler], Handler]
