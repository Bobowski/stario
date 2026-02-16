"""
Telemetry - Lightweight span-based observability.

The Tracer records spans as your application executes operations -
HTTP requests, database queries, API calls - and captures events along the way.

Terminology:
    Span       → An operation with duration and status
    Event      → Point-in-time occurrence within a span (just time, name, attrs)
    Link       → Relates spans via their IDs
    Attributes → Key-value metadata on spans and events

Key Concepts:
    - Spans have status: ok (default) or error (set via span.error = "msg")
    - Events are simple - just time_ns, name, attributes - no status, no IDs
    - Logging exceptions to events does NOT set error status
    - Error status is ONLY set explicitly via span.error

Core API:
    span("event")        → Create event (stored in span.events list)
    span("event", body=) → Create event with content payload
    span[key]            → Get/set attribute
    span.step(name)      → Create child span
    span.root(name)      → Create new detached root span
    span.error = x       → Set error status
"""

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, overload
from uuid import UUID, uuid7


class Tracer(Protocol):
    """
    The tracer that collects and exports spans.

    Implementations decide WHERE spans go:
    - RichTracer: Pretty console output (development)
    - JsonTracer: Structured JSON to stdout (production)
    - OTLPTracer: You'll have to implement this yourself :)

    Usage:
        with RichTracer() as tracer:
            with tracer("http.request") as s:
                s["method"] = "GET"
                ...
    """

    def __call__(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> "Span": ...
    def notify(self, span: "Span") -> None: ...
    def __enter__(self) -> "Tracer": ...
    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...


@dataclass(slots=True)
class Event:
    """
    A point-in-time occurrence within a span.

    Events are simple - just time, name, attributes, and optional body.
    They cannot fail (no status) - only spans have status.
    Events don't have IDs - they're stored in a span's events list.

    Fields:
        attributes → Structured metadata (compact, queryable, filterable)
        body       → Content payload (tracebacks, output, text — human-readable)
    """

    time_ns: int
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    body: Any | None = None


@dataclass(slots=True, frozen=True)
class Link:
    """
    Relates spans via their IDs.

    Use links to connect spans that are related but not parent-child,
    e.g., a retry referencing the original request, or a batch job
    referencing all items it processed.
    """

    span_id: UUID
    attributes: dict[str, Any] = field(default_factory=dict)


class Span:
    """
    An operation with duration, events, and status.

    Core API:
        span("event")        → Create event (stored in span.events)
        span("event", body=) → Create event with content payload
        span(exception)      → Create exception event (body=exc, does NOT set error status)
        span[key]            → Get/set attribute
        span.step(name)      → Create child span
        span.root(name)      → Create new detached root span
        span.error = x       → Set error status (only way to mark span as failed)

    Examples:
        # Create span with events
        with tracer("http.request") as s:
            s["method"] = "GET"
            s("cache.hit")  # Event
            result = handle()
            s["status"] = 200

        # Record exception (does NOT set error status automatically)
        with tracer("db.query") as s:
            try:
                result = db.execute(sql)
            except Exception as e:
                s(e)  # Records exception event
                s.error = str(e)  # THIS sets error status
                raise

        # Child spans
        with tracer("request") as s:
            with s.step("db.query") as db:
                db["sql"] = "SELECT ..."

        # Detached root span
        background_span = s.root("background.task")
    """

    __slots__ = (
        "_tracer",
        "_id",
        "_name",
        "_parent_id",
        "_start_ns",
        "_end_ns",
        "_error",
        "_attrs",
        "_events",
        "_links",
    )

    def __init__(
        self,
        tracer: "Tracer",
        name: str,
        attributes: dict[str, Any] | None = None,
        parent_id: UUID | None = None,
    ):
        self._tracer = tracer
        self._id: UUID = uuid7()
        self._name = name
        self._parent_id = parent_id
        self._start_ns: int = time.time_ns()
        self._end_ns: int | None = None
        self._error: str | None = None  # None = ok, str = error message
        self._attrs: dict[str, Any] = attributes.copy() if attributes else {}
        self._events: list[Event] = []
        self._links: list[Link] = []
        # Notify tracer of new span - automatic registration
        self._tracer.notify(self)

    # -------------------------------------------------------------------------
    # () → Create event OR record exception event
    # -------------------------------------------------------------------------

    @overload
    def __call__(
        self,
        name_or_exc: str,
        attributes: dict[str, Any] | None = None,
        *,
        body: Any | None = None,
    ) -> Event: ...

    @overload
    def __call__(
        self,
        name_or_exc: BaseException,
        attributes: dict[str, Any] | None = None,
    ) -> Event: ...

    def __call__(
        self,
        name_or_exc: str | BaseException,
        attributes: dict[str, Any] | None = None,
        *,
        body: Any | None = None,
    ) -> Event:
        """
        Create an event and add it to this span.

        Events are stored in span.events list.
        Recording an exception does NOT set error status - use span.error for that.

        Args:
            attributes: Structured metadata (compact, queryable)
            body: Content payload (tracebacks, output, text — human-readable)

        Examples:
            span("cache.hit", {"key": "users:123"})
            span("build.step", {"step": 3}, body="RUN apt-get install...")
            span(ValueError("invalid input"))  # Records but doesn't set status
        """
        if isinstance(name_or_exc, BaseException):
            exc = name_or_exc
            attrs = attributes.copy() if attributes else {}
            attrs["exc.type"] = type(exc).__name__
            attrs["exc.message"] = str(exc)
            event = Event(
                time_ns=time.time_ns(),
                name="exception",
                attributes=attrs,
                body=exc,  # Tracer renders: RichTracer uses Traceback, JsonTracer formats frames
            )
        else:
            event = Event(
                time_ns=time.time_ns(),
                name=name_or_exc,
                attributes=attributes.copy() if attributes else {},
                body=body,
            )

        self._events.append(event)
        self._tracer.notify(self)
        return event

    # -------------------------------------------------------------------------
    # Child spans: step() and root()
    # -------------------------------------------------------------------------

    def step(self, name: str, attributes: dict[str, Any] | None = None) -> "Span":
        """
        Create a child span under this one.

        The child shares the same tracer and has this span as parent.

        Example:
            with tracer("request") as s:
                with s.step("db.query") as db:
                    db["sql"] = "SELECT ..."
        """
        return Span(self._tracer, name, attributes, parent_id=self._id)

    def root(self, name: str, attributes: dict[str, Any] | None = None) -> "Span":
        """
        Create a new detached root span.

        The new span has no parent - it's a completely independent trace.
        Useful for background tasks spawned from a request.

        Example:
            with tracer("request") as s:
                background = s.root("background.task")
                # background runs independently
        """
        return Span(self._tracer, name, attributes, parent_id=None)

    # -------------------------------------------------------------------------
    # Links
    # -------------------------------------------------------------------------

    def link(
        self, span_or_id: "Span | UUID", attributes: dict[str, Any] | None = None
    ) -> Link:
        """
        Add a link to another span.

        Links relate spans that are connected but not parent-child.

        Example:
            retry_span.link(original_span, {"retry_count": 3})
        """
        span_id = span_or_id.id if isinstance(span_or_id, Span) else span_or_id
        link = Link(span_id=span_id, attributes=attributes or {})
        self._links.append(link)
        self._tracer.notify(self)
        return link

    # -------------------------------------------------------------------------
    # [] → Get/set attributes
    # -------------------------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        """Get an attribute value."""
        return self._attrs.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        """Set an attribute value."""
        self._attrs[key] = value
        self._tracer.notify(self)

    def __delitem__(self, key: str) -> None:
        """Remove an attribute."""
        del self._attrs[key]
        self._tracer.notify(self)

    def __contains__(self, key: str) -> bool:
        """Check if an attribute exists."""
        return key in self._attrs

    def get(self, key: str, default: Any = None) -> Any:
        """Get an attribute with a default."""
        return self._attrs.get(key, default)

    # -------------------------------------------------------------------------
    # Context manager
    # -------------------------------------------------------------------------

    def __enter__(self) -> "Span":
        """Enter span context."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context - record end time, capture any exception as event."""
        if exc_val is not None:
            self(exc_val)  # Record exception as event
            if self._error is None:
                self._error = str(exc_val)  # Mark span as failed
        self._end_ns = time.time_ns()
        self._tracer.notify(self)

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def id(self) -> UUID:
        """Unique identifier for this span."""
        return self._id

    @property
    def name(self) -> str:
        """The span name."""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        """Rename the span."""
        self._name = value
        self._tracer.notify(self)

    @property
    def parent_id(self) -> UUID | None:
        """Parent span ID, if any."""
        return self._parent_id

    @property
    def attributes(self) -> dict[str, Any]:
        """All attributes."""
        return self._attrs

    @property
    def events(self) -> list[Event]:
        """All events in this span."""
        return self._events

    @property
    def links(self) -> list[Link]:
        """All links from this span."""
        return self._links

    # -------------------------------------------------------------------------
    # Time properties
    # -------------------------------------------------------------------------

    @property
    def start_ns(self) -> int:
        """Start time in nanoseconds."""
        return self._start_ns

    @property
    def end_ns(self) -> int | None:
        """End time in nanoseconds (None if not finished)."""
        return self._end_ns

    @property
    def duration_ns(self) -> int | None:
        """Duration in nanoseconds (None if not finished)."""
        if self._end_ns is None:
            return None
        return self._end_ns - self._start_ns

    # -------------------------------------------------------------------------
    # Status properties
    # -------------------------------------------------------------------------

    @property
    def error(self) -> str | None:
        """
        Error message if span failed, None if ok.

        Set this to mark the span as failed:
            span.error = "Connection refused"
        """
        return self._error

    @error.setter
    def error(self, value: str) -> None:
        """Set error status. This is the ONLY way to mark a span as failed."""
        self._error = value
        self._tracer.notify(self)

    @property
    def ok(self) -> bool:
        """True if no error set."""
        return self._error is None

    @property
    def failed(self) -> bool:
        """True if error set."""
        return self._error is not None

    @property
    def in_progress(self) -> bool:
        """True if not yet finished."""
        return self._end_ns is None

    @property
    def finished(self) -> bool:
        """True if finished."""
        return self._end_ns is not None

    # -------------------------------------------------------------------------
    # Methods
    # -------------------------------------------------------------------------

    def end(self) -> None:
        """
        Manually end the span.

        Usually you'd use `with span:` instead, but this is useful
        when you can't use a context manager.
        """
        if self._end_ns is not None:
            return  # Already ended
        self._end_ns = time.time_ns()
        self._tracer.notify(self)
