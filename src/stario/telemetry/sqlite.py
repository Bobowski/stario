"""Persists finished spans to SQLite for later querying—useful for local debugging, not a distributed trace store."""

import json
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid7

from .core import Span
from .tracebacks import format_exception_for_telemetry

_logger = logging.getLogger("stario.telemetry.sqlite")


class _SqliteState:
    __slots__ = (
        "id",
        "root_id",
        "depth",
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
        root_id: UUID,
        depth: int,
        name: str,
        parent_id: UUID | None,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.id = span_id
        self.root_id = root_id
        self.depth = depth
        self.name = name
        self.parent_id = parent_id
        self.start_ns = 0
        self.end_ns: int | None = None
        self.error: str | None = None
        self.attrs: dict[str, Any] | None = attributes.copy() if attributes else None
        self.events: list[_SqliteEvent] | None = None
        self.links: list[_SqliteLink] | None = None


class _SqliteEvent:
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


class _SqliteLink:
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


def _serialize_body(body: Any) -> str | None:
    """Serialize event body to a stable text value."""
    if body is None:
        return None
    if isinstance(body, BaseException):
        return format_exception_for_telemetry(body)
    if isinstance(body, str):
        return body
    return str(body)


def _serialize_json(value: Any) -> str | None:
    """Serialize a JSON payload or return NULL for empty values."""
    if value is None:
        return None
    return json.dumps(
        value,
        default=_json_default,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _uuid_blob(value: UUID | None) -> bytes | None:
    """Store UUIDs compactly in SQLite."""
    if value is None:
        return None
    return value.bytes


def _coerce_int(value: Any) -> int | None:
    """Coerce known numeric values into integers for indexed columns."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SqliteTracer:
    """Local SQLite span store: in-memory state on the hot path, one writer thread batches commits under ``with tracer``."""

    __slots__ = (
        "_path",
        "_control_lock",
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
        "_writer_error_count",
        "_last_writer_error",
        "_last_writer_error_at_ns",
        "_status_dirty",
        "_spans_lock",
    )

    def __init__(
        self,
        path: str | Path = "stario-traces.sqlite3",
        *,
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

        self._path = Path(path)
        self._control_lock = threading.Lock()
        self._flush_interval = flush_interval
        self._max_pending_spans = max_pending_spans
        self._wake_batch_spans = min(max_batch_spans, max_pending_spans)
        self._spans: dict[UUID, _SqliteState] = {}
        self._pending_lock = threading.Lock()
        self._pending: list[_SqliteState] = []
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._dropped_spans = 0
        self._writer_error_count = 0
        self._last_writer_error: str | None = None
        self._last_writer_error_at_ns: int | None = None
        self._status_dirty = False
        self._spans_lock = threading.Lock()

    def __enter__(self) -> "SqliteTracer":
        self._start_writer()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._stop_writer()

    @property
    def dropped_spans(self) -> int:
        """Number of spans dropped because the pending queue was full."""
        with self._pending_lock:
            return self._dropped_spans

    @property
    def writer_error(self) -> str | None:
        """Last writer error observed by the background thread."""
        with self._pending_lock:
            return self._last_writer_error

    @property
    def writer_error_count(self) -> int:
        """Total number of background writer errors observed."""
        with self._pending_lock:
            return self._writer_error_count

    def stats(self) -> dict[str, int | str | None]:
        """Return tracer health counters for in-process inspection."""
        with self._pending_lock:
            return {
                "dropped_spans": self._dropped_spans,
                "writer_error_count": self._writer_error_count,
                "last_writer_error": self._last_writer_error,
                "last_writer_error_at_ns": self._last_writer_error_at_ns,
            }

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
        with self._spans_lock:
            root_id, depth = self._resolve_lineage(span.id, parent_id)
            state = _SqliteState(
                span.id,
                root_id,
                depth,
                name,
                parent_id,
                attributes=attributes,
            )
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
                event = _SqliteEvent(
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
                link = _SqliteLink(
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
        self._write_batch_sync([state])

    def _start_writer(self) -> None:
        with self._control_lock:
            if self._running:
                return
            self._prepare_database()
            self._running = True
            self._thread = threading.Thread(
                target=self._writer_loop,
                name="stario-sqlite-tracer",
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
            self._write_batch_sync(pending)

    def _enqueue_finished(self, state: _SqliteState) -> None:
        should_wake = False
        with self._pending_lock:
            if len(self._pending) >= self._max_pending_spans:
                self._dropped_spans += 1
                self._status_dirty = True
                should_wake = True
                _logger.warning(
                    "SqliteTracer pending queue full (max=%s); dropping finished span %s (%r)",
                    self._max_pending_spans,
                    state.id,
                    state.name,
                )
                return
            self._pending.append(state)
            should_wake = len(self._pending) >= self._wake_batch_spans
        if should_wake:
            self._wake.set()

    def _take_pending(self) -> list[_SqliteState]:
        with self._pending_lock:
            if not self._pending:
                return []
            pending = self._pending
            self._pending = []
            return pending

    def _writer_loop(self) -> None:
        connection: sqlite3.Connection | None = None
        retry_batch: list[_SqliteState] = []
        shutdown_error_seen = False
        try:
            while True:
                if connection is None:
                    try:
                        connection = self._open_connection()
                        shutdown_error_seen = False
                    except Exception as exc:
                        self._record_writer_error(exc)
                        self._emit_writer_error(exc)
                        if not self._running:
                            if shutdown_error_seen:
                                return
                            shutdown_error_seen = True
                            continue
                        time.sleep(self._flush_interval)
                        continue

                if self._running:
                    self._wake.wait(self._flush_interval)
                    self._wake.clear()
                else:
                    self._wake.clear()

                pending = retry_batch
                queued = self._take_pending()
                if queued:
                    if pending:
                        pending.extend(queued)
                    else:
                        pending = queued

                try:
                    if pending:
                        self._write_batch(connection, pending)
                        retry_batch = []
                    else:
                        retry_batch = pending
                    self._flush_status_if_dirty(connection)
                    shutdown_error_seen = False
                except Exception as exc:
                    retry_batch = pending
                    self._record_writer_error(exc)
                    self._emit_writer_error(exc)
                    connection.close()
                    connection = None
                    if not self._running:
                        if shutdown_error_seen:
                            return
                        shutdown_error_seen = True
                    continue

                if not self._running:
                    pending = self._take_pending()
                    if pending:
                        retry_batch.extend(pending)
                    if retry_batch:
                        try:
                            self._write_batch(connection, retry_batch)
                            retry_batch = []
                        except Exception as exc:
                            self._record_writer_error(exc)
                            self._emit_writer_error(exc)
                            return
                    try:
                        self._flush_status_if_dirty(connection)
                    except Exception as exc:
                        self._record_writer_error(exc)
                        self._emit_writer_error(exc)
                    return
        finally:
            if connection is not None:
                connection.close()

    def _write_batch_sync(self, states: list[_SqliteState]) -> None:
        connection = self._open_connection()
        try:
            self._write_batch(connection, states)
            self._flush_status_if_dirty(connection)
        finally:
            connection.close()

    def _prepare_database(self) -> None:
        connection = self._open_connection()
        connection.close()

    def _resolve_lineage(self, span_id: UUID, parent_id: UUID | None) -> tuple[UUID, int]:
        """Resolve the trace root and nesting depth at creation time."""
        if parent_id is None:
            return span_id, 0
        if parent := self._spans.get(parent_id):
            return parent.root_id, parent.depth + 1
        return parent_id, 1

    def _open_connection(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema(connection)
        connection.commit()
        return connection

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS spans (
                span_id BLOB PRIMARY KEY,
                trace_id BLOB NOT NULL,
                parent_id BLOB,
                depth INTEGER NOT NULL DEFAULT 0,
                name TEXT NOT NULL,
                start_ns INTEGER NOT NULL,
                end_ns INTEGER NOT NULL,
                duration_ns INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                request_method TEXT,
                request_path TEXT,
                response_status_code INTEGER,
                attributes_json TEXT
            ) WITHOUT ROWID
        """)
        self._ensure_columns(
            connection,
            "spans",
            (
                ("trace_id", "BLOB"),
                ("depth", "INTEGER NOT NULL DEFAULT 0"),
                ("request_method", "TEXT"),
                ("request_path", "TEXT"),
                ("response_status_code", "INTEGER"),
            ),
        )
        connection.execute("""
            UPDATE spans
            SET
                trace_id = COALESCE(trace_id, span_id),
                depth = COALESCE(depth, CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END)
            WHERE trace_id IS NULL OR depth IS NULL
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS spans_trace_id_start_ns_idx
            ON spans (trace_id, start_ns)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS spans_end_ns_idx
            ON spans (end_ns)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS spans_error_end_ns_idx
            ON spans (end_ns)
            WHERE status = 'error'
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_status (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                updated_at_ns INTEGER NOT NULL,
                dropped_spans INTEGER NOT NULL DEFAULT 0,
                writer_error_count INTEGER NOT NULL DEFAULT 0,
                last_writer_error TEXT,
                last_writer_error_at_ns INTEGER
            ) WITHOUT ROWID
        """)
        connection.execute("""
            INSERT OR IGNORE INTO telemetry_status (
                singleton,
                updated_at_ns,
                dropped_spans,
                writer_error_count,
                last_writer_error,
                last_writer_error_at_ns
            ) VALUES (1, 0, 0, 0, NULL, NULL)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS span_events (
                span_id BLOB NOT NULL,
                event_index INTEGER NOT NULL,
                time_ns INTEGER NOT NULL,
                name TEXT NOT NULL,
                attributes_json TEXT,
                body TEXT,
                PRIMARY KEY (span_id, event_index)
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS span_links (
                span_id BLOB NOT NULL,
                link_index INTEGER NOT NULL,
                target_span_id BLOB NOT NULL,
                attributes_json TEXT,
                PRIMARY KEY (span_id, link_index)
            ) WITHOUT ROWID
        """)
        self._drop_indexes(
            connection,
            (
                "spans_parent_id_idx",
                "spans_status_end_ns_idx",
                "spans_name_end_ns_idx",
                "spans_duration_ns_idx",
                "spans_response_status_end_ns_idx",
                "spans_request_method_path_end_ns_idx",
                "span_events_name_time_ns_idx",
                "span_events_span_id_time_ns_idx",
                "span_links_target_span_id_idx",
            ),
        )
        connection.execute("PRAGMA user_version=2")

    def _ensure_columns(
        self,
        connection: sqlite3.Connection,
        table: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        existing = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in columns:
            if name in existing:
                continue
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _drop_indexes(
        self,
        connection: sqlite3.Connection,
        index_names: tuple[str, ...],
    ) -> None:
        for index_name in index_names:
            connection.execute(f"DROP INDEX IF EXISTS {index_name}")

    def _record_writer_error(self, exc: Exception) -> None:
        """Remember the latest writer failure for later inspection."""
        with self._pending_lock:
            self._writer_error_count += 1
            self._last_writer_error = f"{type(exc).__name__}: {exc}"
            self._last_writer_error_at_ns = time.time_ns()
            self._status_dirty = True

    def _emit_writer_error(self, exc: Exception) -> None:
        """Surface writer failures immediately for operators."""
        try:
            sys.stderr.write(
                "[stario] sqlite tracer writer error: "
                f"{type(exc).__name__}: {exc}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

    def _flush_status_if_dirty(self, connection: sqlite3.Connection) -> None:
        """Persist tracer health counters when they change."""
        with self._pending_lock:
            if not self._status_dirty:
                return
            updated_at_ns = time.time_ns()
            dropped_spans = self._dropped_spans
            writer_error_count = self._writer_error_count
            last_writer_error = self._last_writer_error
            last_writer_error_at_ns = self._last_writer_error_at_ns

        try:
            with connection:
                connection.execute(
                    """
                    UPDATE telemetry_status
                    SET
                        updated_at_ns = ?,
                        dropped_spans = ?,
                        writer_error_count = ?,
                        last_writer_error = ?,
                        last_writer_error_at_ns = ?
                    WHERE singleton = 1
                    """,
                    (
                        updated_at_ns,
                        dropped_spans,
                        writer_error_count,
                        last_writer_error,
                        last_writer_error_at_ns,
                    ),
                )
        except Exception:
            with self._pending_lock:
                self._status_dirty = True
            raise

        with self._pending_lock:
            if (
                self._dropped_spans == dropped_spans
                and self._writer_error_count == writer_error_count
                and self._last_writer_error == last_writer_error
                and self._last_writer_error_at_ns == last_writer_error_at_ns
            ):
                self._status_dirty = False

    def _write_batch(
        self,
        connection: sqlite3.Connection,
        states: list[_SqliteState],
    ) -> None:
        """Write one finished batch in a single transaction."""
        span_rows: list[tuple[Any, ...]] = []
        event_rows: list[tuple[Any, ...]] = []
        link_rows: list[tuple[Any, ...]] = []

        for state in states:
            if state.end_ns is None:
                continue

            try:
                attrs = state.attrs or {}
                span_rows.append(
                    (
                        _uuid_blob(state.id),
                        _uuid_blob(state.root_id),
                        _uuid_blob(state.parent_id),
                        state.depth,
                        state.name,
                        state.start_ns,
                        state.end_ns,
                        state.end_ns - state.start_ns,
                        "error" if state.error else "ok",
                        state.error,
                        attrs.get("request.method"),
                        attrs.get("request.path"),
                        _coerce_int(attrs.get("response.status_code")),
                        _serialize_json(state.attrs),
                    )
                )

                if state.events:
                    for index, event in enumerate(state.events):
                        event_rows.append(
                            (
                                _uuid_blob(state.id),
                                index,
                                event.time_ns,
                                event.name,
                                _serialize_json(event.attributes),
                                _serialize_body(event.body),
                            )
                        )

                if state.links:
                    for index, link in enumerate(state.links):
                        link_rows.append(
                            (
                                _uuid_blob(state.id),
                                index,
                                _uuid_blob(link.span_id),
                                _serialize_json(link.attributes),
                            )
                        )
            except Exception:
                _logger.exception(
                    "SqliteTracer failed to prepare span row for %s (%r); span omitted from batch",
                    state.id,
                    state.name,
                )
                continue

        if not span_rows:
            return

        with connection:
            connection.executemany(
                """
                INSERT INTO spans (
                    span_id,
                    trace_id,
                    parent_id,
                    depth,
                    name,
                    start_ns,
                    end_ns,
                    duration_ns,
                    status,
                    error,
                    request_method,
                    request_path,
                    response_status_code,
                    attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                span_rows,
            )
            if event_rows:
                connection.executemany(
                    """
                    INSERT INTO span_events (
                        span_id,
                        event_index,
                        time_ns,
                        name,
                        attributes_json,
                        body
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    event_rows,
                )
            if link_rows:
                connection.executemany(
                    """
                    INSERT INTO span_links (
                        span_id,
                        link_index,
                        target_span_id,
                        attributes_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    link_rows,
                )
