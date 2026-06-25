"""Concrete `Span` implementations used by bundled tracers and tests."""

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .core import Attributes, EventBody, Span, Tracer
from .formatters import format_exception_for_telemetry, serialize_event_body


@dataclass(slots=True, eq=False)
class NoOpSpan:
    """Discards all span data; returned by every `NoOpTracer.create()`."""

    id: UUID
    trace_id: UUID
    parent_id: UUID | None

    @property
    def tracer(self) -> Tracer:
        from .noop import NOOP_TRACER

        return NOOP_TRACER

    def start(self) -> None:
        pass

    def __enter__(self) -> Span:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def event(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        pass

    def exception(
        self,
        exc: BaseException,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        pass

    def attr(self, name: str, value: Any) -> None:
        pass

    def attrs(self, attributes: Attributes) -> None:
        pass

    def __setitem__(self, name: str, value: Any) -> None:
        pass

    def step(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self

    def new_trace(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self

    def link(
        self,
        name: str,
        span_id: UUID,
        attributes: Attributes | None = None,
        /,
    ) -> None:
        pass

    def fail(self, message: str) -> None:
        pass

    def end(self) -> None:
        pass


class ProxySpan:
    """Stable span handle whose target can be swapped (bootstrap shutdown span).

    Delegates the full `Span` protocol to the current underlying span. `replace()`
    is the only extra surface — used when one variable must outlive a single span
    record (for example `server.startup` → `server.shutdown`).
    """

    __slots__ = ("_span",)

    def __init__(self, span: Span) -> None:
        self._span = span

    def replace(self, span: Span) -> None:
        self._span = span

    @property
    def id(self) -> UUID:
        return self._span.id

    @property
    def trace_id(self) -> UUID:
        return self._span.trace_id

    @property
    def parent_id(self) -> UUID | None:
        return self._span.parent_id

    @property
    def tracer(self) -> Tracer:
        return self._span.tracer

    def start(self) -> None:
        self._span.start()

    def __enter__(self) -> Span:
        self._span.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._span.__exit__(exc_type, exc_val, exc_tb)

    def event(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        self._span.event(name, attributes, body=body)

    def exception(
        self,
        exc: BaseException,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        self._span.exception(exc, attributes, body=body)

    def attr(self, name: str, value: Any) -> None:
        self._span.attr(name, value)

    def attrs(self, attributes: Attributes) -> None:
        self._span.attrs(attributes)

    def __setitem__(self, name: str, value: Any) -> None:
        self._span[name] = value

    def step(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self._span.step(name, attributes)

    def new_trace(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self._span.new_trace(name, attributes)

    def link(
        self,
        name: str,
        span_id: UUID,
        attributes: Attributes | None = None,
        /,
    ) -> None:
        self._span.link(name, span_id, attributes)

    def fail(self, message: str) -> None:
        self._span.fail(message)

    def end(self) -> None:
        self._span.end()


@dataclass(slots=True)
class RecordedEvent:
    """One in-span timestamped event (exported with the parent span)."""

    time_ns: int
    name: str
    attributes: dict[str, Any] | None = None
    body: str | None = None


@dataclass(slots=True)
class RecordedLink:
    """Reference from this span to another span id (exported with the parent)."""

    name: str
    span_id: UUID
    attributes: dict[str, Any] | None = None


@dataclass(slots=True, eq=False)
class RecordingSpan:
    """Mutable span record; the creating tracer reads it in `on_end()`.

    Implements the `Span` protocol. `tracer` is the sink that created this span
    (`step()` / `new_trace()` call back into it). State properties (`started`,
    `in_progress`, `finished`, `failed`) mirror the lifecycle described on
    `Span` in `core.py`.
    """

    id: UUID
    tracer: Tracer
    trace_id: UUID
    parent_id: UUID | None
    name: str
    start_ns: int | None = None
    end_ns: int | None = None
    error: str | None = None
    attributes: dict[str, Any] | None = None
    events: list[RecordedEvent] | None = None
    links: list[RecordedLink] | None = field(default=None, repr=False)

    @property
    def started(self) -> bool:
        return self.start_ns is not None

    @property
    def duration_ns(self) -> int | None:
        if self.start_ns is None or self.end_ns is None:
            return None
        return self.end_ns - self.start_ns

    @property
    def in_progress(self) -> bool:
        return self.end_ns is None and self.started

    @property
    def finished(self) -> bool:
        return self.end_ns is not None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def start(self) -> None:
        if self.start_ns is None:
            self.start_ns = time.time_ns()

    def __enter__(self) -> Span:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_val is not None:
            self.exception(exc_val)
            self.fail(str(exc_val))
        self.end()

    def event(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        if self.start_ns is None or self.end_ns is not None:
            return
        event = RecordedEvent(
            time.time_ns(),
            name,
            attributes=dict(attributes) if attributes else None,
            body=serialize_event_body(body),
        )
        if self.events is None:
            self.events = [event]
        else:
            self.events.append(event)

    def exception(
        self,
        exc: BaseException,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None:
        if self.start_ns is None or self.end_ns is not None:
            return
        attrs = dict(attributes) if attributes else {}
        attrs["exc.type"] = type(exc).__name__
        attrs["exc.message"] = str(exc)
        if body is None:
            body = format_exception_for_telemetry(exc)
        self.event("exception", attrs, body=body)

    def attr(self, name: str, value: Any) -> None:
        if self.end_ns is not None:
            return
        if self.attributes is None:
            self.attributes = {name: value}
        else:
            self.attributes[name] = value

    def attrs(self, attributes: Attributes) -> None:
        if self.end_ns is not None:
            return
        if not attributes:
            return
        if self.attributes is None:
            self.attributes = dict(attributes)
        else:
            self.attributes.update(attributes)

    def __setitem__(self, name: str, value: Any) -> None:
        self.attr(name, value)

    def step(self, name: str, attributes: Attributes | None = None, /) -> Span:
        if not self.in_progress:
            raise RuntimeError(
                "Cannot create a child span from a span that is not open."
            )
        return self.tracer.create(name, attributes, parent=self)

    def new_trace(self, name: str, attributes: Attributes | None = None, /) -> Span:
        return self.tracer.create(name, attributes)

    def link(
        self,
        name: str,
        span_id: UUID,
        attributes: Attributes | None = None,
        /,
    ) -> None:
        if self.start_ns is None or self.end_ns is not None:
            return
        link = RecordedLink(
            name,
            span_id,
            attributes=dict(attributes) if attributes else None,
        )
        if self.links is None:
            self.links = [link]
        else:
            self.links.append(link)

    def fail(self, message: str) -> None:
        if self.start_ns is None or self.end_ns is not None:
            return
        self.error = message

    def end(self) -> None:
        if self.end_ns is not None:
            return
        if self.start_ns is None:
            raise RuntimeError(
                "Cannot end a span that was never started. "
                "Call span.start() or use the span as a context manager."
            )
        self.end_ns = time.time_ns()
        self.tracer.on_end(self)
