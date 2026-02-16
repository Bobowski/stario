"""
JSON Tracer - Structured logging for production.

Outputs each finished span as a single JSON line to stdout.
Designed for log aggregators (Datadog, Loki, CloudWatch, etc.).

Architecture:
    - Immediate: Spans written the moment they finish
    - Stateless: No buffering, no memory accumulation
    - Thread-safe: Lock-protected writes for concurrent completion
    - Crash-safe: Flush after each span preserves data on crash
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from .core import Span


def _json_default(obj: Any) -> Any:
    """
    Default serializer for non-JSON-serializable values.

    Everything non-serializable: cast to str().
    """
    return str(obj)


def _serialize_body(body: Any) -> Any:
    """
    Serialize event body for JSON output.

    - Exceptions: format as stacktrace string
    - Strings: pass through
    - Everything else: cast to str()
    """
    if body is None:
        return None
    if isinstance(body, BaseException):
        tb = getattr(body, "__traceback__", None)
        if tb:
            return "".join(traceback.format_exception(type(body), body, tb))
        return str(body)
    if isinstance(body, str):
        return body
    return str(body)


def _serialize_span(span: Span) -> dict[str, Any]:
    """Serialize a finished span to dict."""
    result: dict[str, Any] = {
        "span_id": str(span.id),
        "name": span.name,
        "start_ns": span.start_ns,
        "end_ns": span.end_ns,
        "duration_ns": span.duration_ns,
        "status": "error" if span.error else "ok",
    }

    if span.parent_id is not None:
        result["parent_id"] = str(span.parent_id)

    if span.error:
        result["error"] = span.error

    if span.attributes:
        result["attributes"] = dict(span.attributes)

    if span.events:
        events = []
        for e in span.events:
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

    if span.links:
        result["links"] = [
            {
                "span_id": str(ln.span_id),
                "attributes": dict(ln.attributes) if ln.attributes else {},
            }
            for ln in span.links
        ]

    return result


class JsonTracer:
    """
    Production JSON tracer with immediate span output.

    Writes each finished span as a JSON line to stdout.
    Thread-safe and stateless.

    Usage:
        with JsonTracer() as tracer:
            with tracer("http.request") as span:
                span["method"] = "GET"
                with span.step("db.query") as db:
                    db["sql"] = "SELECT ..."
    """

    __slots__ = ("_output", "_lock", "_flush_each")

    def __init__(
        self,
        output: TextIO | None = None,
        *,
        flush_each: bool = True,
    ) -> None:
        self._output: TextIO = output if output is not None else sys.stdout
        self._lock = threading.Lock()
        self._flush_each = flush_each

    def __enter__(self) -> JsonTracer:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        with self._lock:
            self._output.flush()

    def __call__(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        """Create a new root span."""
        from .core import Span

        return Span(self, name, attributes)

    def notify(self, span: Span) -> None:
        """Only finished spans are written to output."""
        if not span.finished:
            return

        try:
            data = _serialize_span(span)
            line = json.dumps(
                data,
                default=_json_default,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except Exception:
            return

        with self._lock:
            self._output.write(line)
            self._output.write("\n")
            if self._flush_each:
                self._output.flush()
