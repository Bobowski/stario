import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, overload

from stario.datastar.parse import parse_signals
from stario.telemetry.core import Event, Span

from .request import Request
from .writer import Writer

if TYPE_CHECKING:
    from .app import Stario


@dataclass(slots=True)
class Context:
    """Context for HTTP requests."""

    app: Stario
    req: Request
    span: Span
    state: dict[str, Any]
    _span_stack: list[Span] = field(default_factory=list, repr=False)

    # =========================================================================
    # Telemetry convenience (explicit, no ambient "magic")
    # =========================================================================

    @property
    def current(self) -> Span:
        """The current span target (request span if not in a step)."""
        if self._span_stack:
            return self._span_stack[-1]
        return self.span

    @overload
    def __call__(
        self, name_or_exc: str, attributes: dict[str, Any] | None = None
    ) -> Event: ...

    @overload
    def __call__(
        self, name_or_exc: BaseException, attributes: dict[str, Any] | None = None
    ) -> Event: ...

    def __call__(
        self,
        name_or_exc: str | BaseException,
        attributes: dict[str, Any] | None = None,
    ) -> Event:
        """
        Record an event (or exception) on the *current* span.

        The request-level span is always available as `c.span`.
        """
        return self.current(name_or_exc, attributes)

    def __setitem__(self, key: str, value: Any) -> None:
        """Set an attribute on the *current* span."""
        self.current[key] = value

    @contextmanager
    def step(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Iterator[Span]:
        """
        Create a child span and make it current within the block.

        Example:
            with c.step("db.query", {"sql": "..."}) as s:
                c("cache.miss")
                c["rows"] = 10
        """
        child = self.current.step(name, attributes)
        self._span_stack.append(child)
        try:
            with child:
                yield child
        finally:
            # Always pop even if the child span recorded an exception in __exit__
            if self._span_stack and self._span_stack[-1] is child:
                self._span_stack.pop()
            else:
                # Defensive: keep stack consistent even if mis-nested.
                try:
                    self._span_stack.remove(child)
                except ValueError:
                    pass

    # =========================================================================
    # Datastar Signals
    # =========================================================================

    @overload
    async def signals(self) -> dict[str, Any]: ...

    @overload
    async def signals[T](self, schema: type[T]) -> T: ...

    async def signals[T](self, schema: type[T] | None = None) -> T | dict[str, Any]:
        """Get Datastar signals from request."""
        if self.req.method == "GET":
            # GET: signals in query string
            data = self.req.query.get("datastar")
            if data is not None:
                signals_data = json.loads(data)
            else:
                signals_data = {}
        else:
            # POST/PUT/etc: signals in JSON body
            try:
                signals_data = await self.req.json()
            except (json.JSONDecodeError, ValueError):
                signals_data = {}

        if schema is None:
            return signals_data

        return parse_signals(signals_data, schema)


type Handler = Callable[[Context, Writer], Awaitable[None]]

type Middleware = Callable[[Handler], Handler]

type ErrorHandler[E: Exception] = Callable[[Context, Writer, E], None | Awaitable[None]]
