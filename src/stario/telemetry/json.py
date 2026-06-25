"""Emits finished spans as newline-delimited JSON for log pipelines and collectors."""

import sys
import threading
from typing import Any, TextIO

from stario._env import env_float, env_int

from .buffered import _BufferedTracer
from .formatters import dumps_json
from .spans import RecordingSpan

_DEFAULT_FLUSH_INTERVAL = 0.125
_DEFAULT_MAX_PENDING_SPANS = 65536
_DEFAULT_MAX_BATCH_SPANS = 512


def _serialize_span(span: RecordingSpan) -> dict[str, Any]:
    """Serialize one finished span."""
    if span.start_ns is None or span.end_ns is None:
        raise RuntimeError("JsonTracer can only serialize finished spans")
    result: dict[str, Any] = {
        "span_id": str(span.id),
        "trace_id": str(span.trace_id),
        "name": span.name,
        "start_ns": span.start_ns,
        "end_ns": span.end_ns,
        "duration_ns": span.end_ns - span.start_ns,
        "status": "error" if span.error else "ok",
    }

    if span.parent_id is not None:
        result["parent_id"] = str(span.parent_id)

    if span.error:
        result["error"] = span.error

    if span.attributes:
        result["attributes"] = span.attributes

    if span.events:
        events: list[dict[str, Any]] = []
        for event in span.events:
            ev: dict[str, Any] = {
                "time_ns": event.time_ns,
                "name": event.name,
            }
            if event.attributes:
                ev["attributes"] = event.attributes
            if event.body is not None:
                ev["body"] = event.body
            events.append(ev)
        result["events"] = events

    if span.links:
        links: list[dict[str, Any]] = []
        for link in span.links:
            item: dict[str, Any] = {
                "name": link.name,
                "span_id": str(link.span_id),
            }
            if link.attributes:
                item["attributes"] = link.attributes
            links.append(item)
        result["links"] = links

    return result


class JsonTracer(_BufferedTracer):
    """NDJSON span sink writing one JSON object per line.

    Enter before `create()`. A background thread batches finished spans;
    `flush_each` controls whether each batch flushes the output stream.
    Overflow drops spans and increments `stats().dropped_spans`.
    """

    __slots__ = ("_flush_each", "_output", "_write_lock")

    def __init__(
        self,
        output: TextIO | None = None,
        *,
        flush_each: bool = True,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        max_pending_spans: int = _DEFAULT_MAX_PENDING_SPANS,
        max_batch_spans: int = _DEFAULT_MAX_BATCH_SPANS,
    ) -> None:
        super().__init__(
            tracer_label="json tracer",
            tracer_type_name="JsonTracer",
            flush_interval=flush_interval,
            max_pending_spans=max_pending_spans,
            max_batch_spans=max_batch_spans,
        )
        self._output: TextIO = output if output is not None else sys.stdout
        self._write_lock = threading.Lock()
        self._flush_each = flush_each

    def __enter__(self) -> JsonTracer:
        super().__enter__()
        return self

    def _on_exit(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        try:
            with self._write_lock:
                self._output.flush()
        except Exception as exc:
            self._record_writer_error(exc)
            self._emit_stderr(f"json tracer flush error: {type(exc).__name__}: {exc}")

    def _thread_name(self) -> str:
        return "stario-json-tracer"

    def _write_batch(self, spans: list[RecordingSpan]) -> None:
        """Serialize and write one finished batch."""
        lines: list[str] = []
        for span in spans:
            try:
                data = _serialize_span(span)
                lines.append(dumps_json(data))
            except Exception as exc:
                self._record_serialization_error()
                self._emit_stderr(
                    "json tracer serialization error for span "
                    f"{span.id} ({span.name!r}); "
                    f"span omitted: {type(exc).__name__}: {exc}"
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


def json_tracer_from_env() -> JsonTracer:
    """Build a `JsonTracer` from `STARIO_TRACERS_JSON_*` environment variables."""
    return JsonTracer(
        flush_interval=env_float(
            "STARIO_TRACERS_JSON_FLUSH_INTERVAL", _DEFAULT_FLUSH_INTERVAL
        ),
        max_pending_spans=env_int(
            "STARIO_TRACERS_JSON_MAX_PENDING_SPANS", _DEFAULT_MAX_PENDING_SPANS
        ),
        max_batch_spans=env_int(
            "STARIO_TRACERS_JSON_MAX_BATCH_SPANS", _DEFAULT_MAX_BATCH_SPANS
        ),
    )
