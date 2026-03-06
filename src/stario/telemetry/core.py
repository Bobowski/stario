"""Span-based telemetry primitives."""

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


class Tracer(Protocol):
    """Tracer contract used by `Span` handles.

    `create()` returns a stopped `Span`. Call `span.start()` explicitly or use
    `with tracer.create(... ) as span:` to start timing.
    """

    # Tracer lifecycle (open / close)
    def __enter__(self) -> Tracer: ...
    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...

    # Span creation and management.
    # `create()` allocates a span handle only; it does not start timing.
    def create(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        parent_id: UUID | None = None,
    ) -> Span: ...
    def start(self, span_id: UUID) -> None: ...
    def set_attribute(
        self,
        span_id: UUID,
        name: str,
        value: Any,
    ) -> None: ...
    def set_attributes(
        self,
        span_id: UUID,
        attributes: dict[str, Any],
    ) -> None: ...
    def add_event(
        self,
        span_id: UUID,
        name: str,
        attributes: dict[str, Any] | None = None,
        *,
        body: Any | None = None,
    ) -> None: ...
    def add_link(
        self,
        span_id: UUID,
        target_span_id: UUID,
        attributes: dict[str, Any] | None = None,
    ) -> None: ...
    def set_name(self, span_id: UUID, name: str) -> None: ...
    def fail(self, span_id: UUID, message: str) -> None: ...
    def end(self, span_id: UUID) -> None: ...


@dataclass(slots=True)
class Span:
    """Write-only span handle. State lives in tracer.

    Spans are created in a stopped state. Start them explicitly with `start()`
    or by using the span as a context manager.
    """

    id: UUID
    tracer: Tracer

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """Start span in tracer."""
        self.tracer.start(self.id)

    def __enter__(self) -> Span:
        """Start the span when entering a context manager."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Record exception event if present and end span."""
        if exc_val is not None:
            self.exception(exc_val)
            self.tracer.fail(self.id, str(exc_val))
        self.end()

    # -------------------------------------------------------------------------
    # Span data
    # -------------------------------------------------------------------------

    def event(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        *,
        body: Any | None = None,
    ) -> None:
        """Record an event."""
        self.tracer.add_event(
            self.id,
            name,
            attributes=attributes,
            body=body,
        )

    def exception(
        self,
        exc: BaseException,
        attributes: dict[str, Any] | None = None,
        *,
        body: Any | None = None,
    ) -> None:
        """Record a structured exception event."""
        attrs = attributes.copy() if attributes else {}
        attrs["exc.type"] = type(exc).__name__
        attrs["exc.message"] = str(exc)
        self.tracer.add_event(
            self.id,
            "exception",
            attributes=attrs,
            body=exc if body is None else body,
        )

    def attr(self, name: str, value: Any) -> None:
        """Set one span attribute."""
        self.tracer.set_attribute(self.id, name, value)

    def attrs(self, attributes: dict[str, Any]) -> None:
        """Set many span attributes."""
        if not attributes:
            return
        self.tracer.set_attributes(self.id, attributes)

    def __setitem__(self, name: str, value: Any) -> None:
        """Assignment sugar for setting one attribute."""
        self.attr(name, value)

    # -------------------------------------------------------------------------
    # Child and root spans
    # -------------------------------------------------------------------------

    def step(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        """Create a child span in a stopped state."""
        return self.tracer.create(name, attributes=attributes, parent_id=self.id)

    def create(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        """Create a detached root span in a stopped state."""
        return self.tracer.create(name, attributes=attributes)

    # -------------------------------------------------------------------------
    # Cross-span references
    # -------------------------------------------------------------------------

    def link(
        self, span_or_id: Span | UUID, attributes: dict[str, Any] | None = None
    ) -> None:
        """Link this span to another span ID."""
        target_id = span_or_id.id if isinstance(span_or_id, Span) else span_or_id
        self.tracer.add_link(self.id, target_id, attributes=attributes)

    # -------------------------------------------------------------------------
    # Completion
    # -------------------------------------------------------------------------

    def fail(self, message: str) -> None:
        """Mark span as failed."""
        self.tracer.fail(self.id, message)

    def end(self) -> None:
        """End a started span."""
        self.tracer.end(self.id)
