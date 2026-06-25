"""Shared bounded queue, writer thread, and self-metrics for Json/SQLite sinks."""

import queue
import sys
import threading
import time
from typing import Any, Self, cast
from uuid import uuid7

from .core import Attributes, Span, TelemetryStats, Tracer
from .spans import RecordingSpan

# Subclassed by JsonTracer and SqliteTracer.
__all__ = ["_BufferedTracer"]


class _BufferedTracer:
    """Background writer thread with a bounded pending queue and drop metrics.

    Subclasses implement `_write_batch()` and `_thread_name()`. Not part of the
    public API — use `JsonTracer` or `SqliteTracer`.
    """

    __slots__ = (
        "_control_lock",
        "_dropped_spans",
        "_flush_interval",
        "_last_writer_error",
        "_last_writer_error_at_ns",
        "_max_batch_spans",
        "_max_pending_spans",
        "_metrics_lock",
        "_pending",
        "_running",
        "_serialization_error_count",
        "_thread",
        "_tracer_label",
        "_tracer_type_name",
        "_wake",
        "_wake_batch_spans",
        "_writer_error_count",
    )

    def __init__(
        self,
        *,
        tracer_label: str,
        tracer_type_name: str,
        flush_interval: float,
        max_pending_spans: int,
        max_batch_spans: int,
    ) -> None:
        if flush_interval <= 0:
            raise ValueError("flush_interval must be greater than zero")
        if max_pending_spans <= 0:
            raise ValueError("max_pending_spans must be greater than zero")
        if max_batch_spans <= 0:
            raise ValueError("max_batch_spans must be greater than zero")

        self._tracer_label = tracer_label
        self._tracer_type_name = tracer_type_name
        self._control_lock = threading.Lock()
        self._flush_interval = flush_interval
        self._max_pending_spans = max_pending_spans
        self._max_batch_spans = min(max_batch_spans, max_pending_spans)
        self._wake_batch_spans = self._max_batch_spans
        self._pending: queue.Queue[RecordingSpan] = queue.Queue(
            maxsize=max_pending_spans
        )
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._metrics_lock = threading.Lock()
        self._dropped_spans = 0
        self._serialization_error_count = 0
        self._writer_error_count = 0
        self._last_writer_error: str | None = None
        self._last_writer_error_at_ns: int | None = None

    def __enter__(self: Self) -> Tracer:
        self._start_writer()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._stop_writer()
        self._on_exit(exc_type, exc_val, exc_tb)

    def _on_exit(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Subclass hook after the writer thread stops."""

    def stats(self) -> TelemetryStats:
        with self._metrics_lock:
            return TelemetryStats(
                dropped_spans=self._dropped_spans,
                serialization_error_count=self._serialization_error_count,
                writer_error_count=self._writer_error_count,
                last_writer_error=self._last_writer_error,
                last_writer_error_at_ns=self._last_writer_error_at_ns,
            )

    def create(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> RecordingSpan:
        if not self._running:
            raise RuntimeError(
                f"{self._tracer_type_name} must be entered before creating spans."
            )
        span_id = uuid7()
        if parent is None:
            trace_id = span_id
            parent_id = None
        else:
            trace_id = parent.trace_id
            parent_id = parent.id
        return RecordingSpan(
            span_id,
            cast(Tracer, self),
            trace_id,
            parent_id,
            name,
            attributes=dict(attributes) if attributes else None,
        )

    def on_end(self, span: Span) -> None:
        span = cast(RecordingSpan, span)
        if not self._running:
            self._record_dropped_spans(1)
            self._emit_stderr(
                f"{self._tracer_label} dropped finished span while stopped: "
                f"{span.id} ({span.name!r})"
            )
            return
        self._enqueue_finished(span)

    def _start_writer(self) -> None:
        with self._control_lock:
            if self._running:
                return
            self._on_start_writer()
            self._running = True
            self._thread = threading.Thread(
                target=self._writer_loop,
                name=self._thread_name(),
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
            self._write_batches_safe(pending)

    def _on_start_writer(self) -> None:
        """Subclass hook run once before the writer thread starts."""

    def _thread_name(self) -> str:
        raise NotImplementedError

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait(self._flush_interval)
            self._wake.clear()

            pending = self._take_pending()
            if pending:
                self._write_batches_safe(pending)

            if not self._running:
                pending = self._take_pending()
                if pending:
                    self._write_batches_safe(pending)
                return

    def _enqueue_finished(self, span: RecordingSpan) -> None:
        try:
            self._pending.put_nowait(span)
        except queue.Full:
            self._record_dropped_spans(1)
            self._emit_stderr(
                f"{self._tracer_label} pending queue full "
                f"(max={self._max_pending_spans}); dropping finished span "
                f"{span.id} ({span.name!r})"
            )
            self._wake.set()
            return
        if self._pending.qsize() >= self._wake_batch_spans:
            self._wake.set()

    def _take_pending(self) -> list[RecordingSpan]:
        batch: list[RecordingSpan] = []
        while True:
            try:
                batch.append(self._pending.get_nowait())
            except queue.Empty:
                break
        return batch

    def _record_dropped_spans(self, count: int) -> None:
        if count <= 0:
            return
        with self._metrics_lock:
            self._dropped_spans += count

    def _record_writer_error(self, exc: Exception) -> None:
        with self._metrics_lock:
            self._writer_error_count += 1
            self._last_writer_error = f"{type(exc).__name__}: {exc}"
            self._last_writer_error_at_ns = time.time_ns()

    def _record_serialization_error(self) -> None:
        with self._metrics_lock:
            self._serialization_error_count += 1

    def _emit_stderr(self, message: str) -> None:
        try:
            sys.stderr.write(f"[stario] {message}\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _write_batches_safe(self, spans: list[RecordingSpan]) -> None:
        try:
            self._write_batches(spans)
        except Exception as exc:
            self._record_writer_error(exc)
            self._emit_stderr(
                f"{self._tracer_label} write error for {len(spans)} finished spans: "
                f"{type(exc).__name__}: {exc}"
            )

    def _write_batches(self, spans: list[RecordingSpan]) -> None:
        for start in range(0, len(spans), self._max_batch_spans):
            self._write_batch(spans[start : start + self._max_batch_spans])

    def _write_batch(self, spans: list[RecordingSpan]) -> None:
        raise NotImplementedError
