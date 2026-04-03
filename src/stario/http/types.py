import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from stario.datastar.parse import parse_signals
from stario.telemetry.core import Span

from .request import Request
from .writer import Writer

if TYPE_CHECKING:
    from .app import Stario


class CreateTask(Protocol):
    def __call__[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[T]: ...


type UrlQueryScalar = str | int | float | bool
type UrlQueryValue = UrlQueryScalar | list[UrlQueryScalar] | tuple[UrlQueryScalar, ...]
type UrlQueryParams = Mapping[str, UrlQueryValue]


class UrlFor(Protocol):
    def __call__(
        self,
        name: str,
        path: str | dict[str, str] | None = None,
        queries: UrlQueryParams | None = None,
    ) -> str: ...


@dataclass(slots=True)
class Context:
    """Context for HTTP requests."""

    app: "Stario"
    req: Request
    span: Span
    state: dict[str, Any]
    # Create a server-managed task tied to this worker's shutdown lifecycle.
    create_task: CreateTask = field(repr=False)

    # =========================================================================
    # Datastar Signals
    # =========================================================================

    def url_for(
        self,
        name: str,
        path: str | dict[str, str] | None = None,
        queries: UrlQueryParams | None = None,
    ) -> str:
        """Resolve a named route or asset URL through the current app."""
        return self.app.url_for(name, path, queries=queries)

    async def signals[T](self, schema: type[T] = dict[str, Any]) -> T:
        """Get Datastar signals from request."""

        if self.req.method == "GET":
            raw = self.req.query.get("datastar", "")
        else:
            raw = await self.req.body()

        return parse_signals(raw, schema)


type Handler = Callable[[Context, Writer], Awaitable[None]]

type Middleware = Callable[[Handler], Handler]

type ErrorHandler[E: Exception] = Callable[[Context, Writer, E], None | Awaitable[None]]
