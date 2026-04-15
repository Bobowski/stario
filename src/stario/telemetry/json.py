"""Emits finished spans as newline-delimited JSON suitable for log pipelines and stdout collectors."""

import json
import logging
import sys
import threading
import time
from typing import Any, TextIO
from uuid import UUID, uuid7

from .core import Span
from .tracebacks import format_exception_for_telemetry

_logger = logging.getLogger("stario.telemetry.json")


class _JsonState:
    __slots__ = (
        "id",
        "name",
        "parent_id",
        "start_ns",
        "end_ns",
        "error",
        "attrs",
        "events",
        "links",
    )

    def __init__(
        self,
        span_id: UUID,
        name: str,
        parent_id: UUID | None,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.id = span_id
        self.name = name
        self.parent_id = parent_id
        self.start_ns = 0
        self.end_ns: int | None = None
        self.error: str | None = None
        self.attrs: dict[str, Any] | None = attributes.copy() if attributes else None
        self.events: list[_JsonEvent] | None = None
        self.links: list[_JsonLink] | None = None


class _JsonEvent:
    __slots__ = ("time_ns", "name", "attributes", "body")

    def __init__(
        self,
        time_ns: int,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> None:
        self.time_ns = time_ns
        self.name = name
        self.attributes = attributes
        self.body = body


class _JsonLink:
    __slots__ = ("span_id", "attributes")

    def __init__(
        self,
        span_id: UUID,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.span_id = span_id
        self.attributes = attributes


def _json_default(obj: Any) -> Any:
    """Fallback serializer."""
    return str(obj)


def _serialize_body(body: Any) -> Any:
    """Serialize event body to JSON-safe value."""
    if body is None:
        return None
    if isinstance(body, BaseException):
        return format_exception_for_telemetry(body)
    if isinstance(body, str):
        return body
    return str(body)


def _serialize_state(state: _JsonState) -> dict[str, Any]:
    """Serialize one finished span state."""
    result: dict[str, Any] = {
        "span_id": str(state.id),
        "name": state.name,
        "start_ns": state.start_ns,
        "end_ns": state.end_ns,
        "duration_ns": None if state.end_ns is None else state.end_ns - state.start_ns,
        "status": "error" if state.error else "ok",
    }

    if state.parent_id is not None:
        result["parent_id"] = str(state.parent_id)

    if state.error:
        result["error"] = state.error

    if state.attrs:
        result["attributes"] = dict(state.attrs)

    if state.events:
        events = []
        for e in state.events:
            ev: dict[str, Any] = {
                "time_ns": e.time_ns,
                "name": e.name,
            }
            if e.attributes:
                ev["attributes"] = dict(e.attributes)
            if e.body is not None:
                ev["body"] = _serialize_body(e.body)
            events.append(ev)
        result["events"] = events

    if state.links:
        result["links"] = [
            {
                "span_id": str(ln.span_id),
                "attributes": dict(ln.attributes) if ln.attributes else {},
            }
            for ln in state.links
        ]

    return result


class JsonTracer:
    """NDJSON span sink: under ``with tracer`` a background thread batches flushes; outside, ``end()`` writes synchronously."""

    __slots__ = (
        "_output",
        "_write_lock",
        "_control_lock",
        "_flush_each",
        "_flush_interval",
        "_max_pending_spans",
        "_wake_batch_spans",
        "_spans",
        "_pending_lock",
        "_pending",
        "_wake",
        "_thread",
        "_running",
        "_dropped_spans",
        "_serialize_errors",
        "_spans_lock",
    )

    def __init__(
        self,
        output: TextIO | None = None,
        *,
        flush_each: bool = True,
        flush_interval: float = 0.125,
        max_pending_spans: int = 65536,
        max_batch_spans: int = 512,
    ) -> None:
        if flush_interval <= 0:
            raise ValueError("flush_interval must be greater than zero")
        if max_pending_spans <= 0:
            raise ValueError("max_pending_spans must be greater than zero")
        if max_batch_spans <= 0:
            raise ValueError("max_batch_spans must be greater than zero")

        self._output: TextIO = output if output is not None else sys.stdout
        self._write_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._flush_each = flush_each
        self._flush_interval = flush_interval
        self._max_pending_spans = max_pending_spans
        self._wake_batch_spans = min(max_batch_spans, max_pending_spans)
        self._spans: dict[UUID, _JsonState] = {}
        self._pending_lock = threading.Lock()
        self._pending: list[_JsonState] = []
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._dropped_spans = 0
        self._serialize_errors = 0
        self._spans_lock = threading.Lock()

    def __enter__(self) -> JsonTracer:
        self._start_writer()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._stop_writer()
        with self._write_lock:
            self._output.flush()

    @property
    def serialize_errors(self) -> int:
        """Spans skipped because JSON serialization failed (see ``_write_batch``)."""
        with self._pending_lock:
            return self._serialize_errors

    def create(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        /,
        *,
        parent_id: UUID | None = None,
    ) -> Span:
        """Create a span handle; call ``start()`` or use the span as a context manager."""

        span = Span(id=uuid7(), tracer=self)
        state = _JsonState(
            span.id,
            name,
            parent_id,
            attributes=attributes,
        )
        with self._spans_lock:
            self._spans[span.id] = state
        return span

    def start(self, span_id: UUID) -> None:
        with self._spans_lock:
            if state := self._spans.get(span_id):
                if state.start_ns == 0:
                    state.start_ns = time.time_ns()

    def set_attribute(
        self,
        span_id: UUID,
        name: str,
        value: Any,
    ) -> None:
        with self._spans_lock:
            if state := self._spans.get(span_id):
                if state.attrs is None:
                    state.attrs = {name: value}
                else:
                    state.attrs[name] = value

    def set_attributes(
        self,
        span_id: UUID,
        attributes: dict[str, Any],
    ) -> None:
        with self._spans_lock:
            if attributes and (state := self._spans.get(span_id)):
                if state.attrs is None:
                    state.attrs = attributes.copy()
                else:
                    state.attrs.update(attributes)

    def add_event(
        self,
        span_id: UUID,
        name: str,
        attributes: dict[str, Any] | None = None,
        /,
        *,
        body: Any | None = None,
    ) -> None:
        with self._spans_lock:
            if state := self._spans.get(span_id):
                event = _JsonEvent(
                    time.time_ns(),
                    name,
                    attributes=attributes.copy() if attributes else None,
                    body=body,
                )
                if state.events is None:
                    state.events = [event]
                else:
                    state.events.append(event)

    def add_link(
        self,
        span_id: UUID,
        target_span_id: UUID,
        attributes: dict[str, Any] | None = None,
        /,
    ) -> None:
        with self._spans_lock:
            if state := self._spans.get(span_id):
                link = _JsonLink(
                    target_span_id,
                    attributes=attributes.copy() if attributes else None,
                )
                if state.links is None:
                    state.links = [link]
                else:
                    state.links.append(link)

    def fail(self, span_id: UUID, message: str) -> None:
        with self._spans_lock:
            if state := self._spans.get(span_id):
                state.error = message

    def set_name(self, span_id: UUID, name: str) -> None:
        with self._spans_lock:
            if state := self._spans.get(span_id):
                state.name = name

    def end(self, span_id: UUID) -> None:
        with self._spans_lock:
            if not (state := self._spans.get(span_id)):
                return
            if state.end_ns is not None:
                return
            if state.start_ns == 0:
                raise RuntimeError(
                    "Cannot end a span that was never started. "
                    "Call span.start() or use the span as a context manager."
                )
            self._spans.pop(span_id, None)
        state.end_ns = time.time_ns()
        if self._running:
            self._enqueue_finished(state)
            return
        self._write_batch([state])

    def _start_writer(self) -> None:
        with self._control_lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._writer_loop,
                name="stario-json-tracer",
                daemon=True,
            )
            self._thread.start()

    def _stop_writer(self) -> None:
        with self._control_lock:
            thread = self._thread
            if thread is None:
                return
            self._running = False
            self._thread = None
            self._wake.set()
        thread.join()
        pending = self._take_pending()
        if pending:
            self._write_batch(pending)

    def _enqueue_finished(self, state: _JsonState) -> None:
        should_wake = False
        with self._pending_lock:
            if len(self._pending) >= self._max_pending_spans:
                self._dropped_spans += 1
                _logger.warning(
                    "JsonTracer pending queue full (max=%s); dropping finished span %s (%r)",
                    self._max_pending_spans,
                    state.id,
                    state.name,
                )
                return
            self._pending.append(state)
            should_wake = len(self._pending) >= self._wake_batch_spans
        if should_wake:
            self._wake.set()

    def _take_pending(self) -> list[_JsonState]:
        with self._pending_lock:
            if not self._pending:
                return []
            pending = self._pending
            self._pending = []
            return pending

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait(self._flush_interval)
            self._wake.clear()

            pending = self._take_pending()
            if pending:
                self._write_batch(pending)

            if not self._running:
                pending = self._take_pending()
                if pending:
                    self._write_batch(pending)
                return

    def _write_batch(self, states: list[_JsonState]) -> None:
        """Serialize and write one finished batch."""
        lines: list[str] = []
        append = lines.append
        for state in states:
            try:
                data = _serialize_state(state)
                append(
                    json.dumps(
                        data,
                        default=_json_default,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                )
            except Exception:
                with self._pending_lock:
                    self._serialize_errors += 1
                _logger.exception(
                    "JsonTracer failed to serialize span %s (%r); span omitted from output",
                    state.id,
                    state.name,
                )
                continue

        if not lines:
            return

        payload = "\n".join(lines)
        payload += "\n"
        with self._write_lock:
            self._output.write(payload)
            if self._flush_each:
                self._output.flush()
