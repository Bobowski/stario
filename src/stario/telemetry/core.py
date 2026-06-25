"""
`Tracer` protocol and `Span` handle.

Handlers and bootstrap code call methods on the span from `tracer.create()`
(`.attr`, `.event`, `.step`, and so on). Spans own their lifecycle; tracers
create spans and export finished records on `end()`. Enter a tracer before
creating spans so background writers and sinks are active.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

# Human-readable diagnostics for events: str text, formatted traceback, or omitted.
type EventBody = str | BaseException | None
# Read-only attribute bags passed into span/tracer APIs (stored copies are dicts).
type Attributes = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TelemetryStats:
    """Snapshot of tracer self-observation counters.

    Json and SQLite sinks increment these when spans are dropped, serialization
    fails, or the writer raises. Dev (`TTYTracer`) and noop tracers always
    return zeros.
    """

    dropped_spans: int = 0
    serialization_error_count: int = 0
    writer_error_count: int = 0
    last_writer_error: str | None = None
    last_writer_error_at_ns: int | None = None


class Span(Protocol):
    """Handle for one logical unit of work.

    ## Lifecycle

    `Tracer.create()` allocates a span; the clock does **not** start until
    `start()` or `with span`. `end()` exports the finished record to
    `span.tracer.on_end()` and freezes further mutations.

    ```python
    with tracer.create("request") as span:
        span.attrs({"request.method": "GET"})
    ```

    Manual control is fine when you cannot use a context manager:

    ```python
    span = tracer.create("work")
    span.start()
    span.end()  # RuntimeError if start() was skipped
    ```

    `attr` / `attrs` may be set before `start()`. Timestamped operations
    (`event`, `exception`, `link`, and `fail`) record only while the span is in
    progress; before `start()` and after `end()` they are ignored.

    Uncaught exceptions in `with span` record an `exception` event, call
    `fail(str(exc))`, then `end()`.

    ## Parenting

    Prefer `span.step(name)` for children — the parent must be in progress.
    `tracer.create(..., parent=span)` is the low-level escape hatch used by
    `step()` and `new_trace()`.

    ## Attributes and events

    Structured data belongs in `attr` / `attrs` or event attributes. Values
    should be JSON-serializable; sinks stringify unknown types at export. Event
    `body` is for readable diagnostics only (`str`, `BaseException`, or `None`).
    """

    @property
    def id(self) -> UUID: ...
    @property
    def trace_id(self) -> UUID: ...
    @property
    def parent_id(self) -> UUID | None: ...
    @property
    def tracer(self) -> Tracer: ...

    def start(self) -> None: ...
    def __enter__(self) -> Span: ...
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None: ...

    def event(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None: ...
    def exception(
        self,
        exc: BaseException,
        attributes: Attributes | None = None,
        /,
        *,
        body: EventBody = None,
    ) -> None: ...
    def attr(self, name: str, value: Any) -> None: ...
    def attrs(self, attributes: Attributes) -> None: ...
    def __setitem__(self, name: str, value: Any) -> None: ...
    def step(self, name: str, attributes: Attributes | None = None, /) -> Span: ...
    def new_trace(self, name: str, attributes: Attributes | None = None, /) -> Span: ...
    def link(
        self,
        name: str,
        span_id: UUID,
        attributes: Attributes | None = None,
        /,
    ) -> None: ...
    def fail(self, message: str) -> None: ...
    def end(self) -> None: ...


class Tracer(Protocol):
    """Creates spans and exports finished ones to a sink.

    ## Lifecycle

    Enter the tracer before `create()` so async writers and database handles
    are running. Exit drains pending spans (Json/SQLite) or flushes the TTY
    view.

    Custom factories registered via `STARIO_TRACER=module:callable` must return
    an object implementing this protocol. `create()` should return spans
    compatible with the bundled `RecordingSpan` record shape so `on_end()` can
    read finished fields.
    """

    def __enter__(self) -> Tracer: ...
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None: ...

    def create(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> Span: ...
    def on_end(self, span: Span) -> None: ...
    def stats(self) -> TelemetryStats: ...
