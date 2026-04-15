import json
import sqlite3
import threading
from uuid import uuid4

from stario.telemetry.sqlite import SqliteTracer


def test_sqlite_tracer_context_manager_persists_span_tree(tmp_path) -> None:
    db_path = tmp_path / "traces.sqlite3"
    tracer = SqliteTracer(db_path)

    with tracer.create("request") as span:
        span.attr("request.method", "GET")
        span.attr("request.path", "/")
        with span.step("router.dispatch") as child:
            child.attr("route.name", "home")
        span.event("handler.hit", {"path": "/"}, body={"ok": True})
        span.link(uuid4(), {"kind": "related"})
        span.attr("response.status_code", 200)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                hex(span_id),
                hex(trace_id),
                hex(parent_id),
                depth,
                name,
                status,
                error,
                request_method,
                request_path,
                response_status_code,
                attributes_json,
                length(span_id)
            FROM spans
            ORDER BY depth, start_ns
            """
        ).fetchall()
        assert len(rows) == 2

        root_row, child_row = rows
        assert root_row[3] == 0
        assert root_row[4] == "request"
        assert root_row[5] == "ok"
        assert root_row[6] is None
        assert root_row[7] == "GET"
        assert root_row[8] == "/"
        assert root_row[9] == 200
        assert json.loads(root_row[10]) == {
            "request.method": "GET",
            "request.path": "/",
            "response.status_code": 200,
        }
        assert root_row[11] == 16
        assert root_row[0] == root_row[1]
        assert root_row[2] == ""

        assert child_row[3] == 1
        assert child_row[4] == "router.dispatch"
        assert child_row[7] is None
        assert child_row[8] is None
        assert child_row[9] is None
        assert child_row[1] == root_row[0]
        assert child_row[2] == root_row[0]
        assert json.loads(child_row[10]) == {"route.name": "home"}

        event_row = connection.execute(
            """
            SELECT event_index, name, attributes_json, body
            FROM span_events
            """
        ).fetchone()
        assert event_row == (
            0,
            "handler.hit",
            '{"path":"/"}',
            "{'ok': True}",
        )

        link_row = connection.execute(
            """
            SELECT link_index, length(span_id), length(target_span_id), attributes_json
            FROM span_links
            """
        ).fetchone()
        assert link_row == (0, 16, 16, '{"kind":"related"}')

        user_version = connection.execute("PRAGMA user_version").fetchone()
        assert user_version == (2,)


def test_sqlite_tracer_keeps_only_operational_indexes(tmp_path) -> None:
    db_path = tmp_path / "traces.sqlite3"
    tracer = SqliteTracer(db_path)

    with tracer.create("request") as span:
        span.event("event")
        span.link(uuid4())

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS spans_parent_id_idx ON spans (parent_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS spans_status_end_ns_idx ON spans (status, end_ns)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS spans_name_end_ns_idx ON spans (name, end_ns)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS spans_duration_ns_idx ON spans (duration_ns)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS spans_response_status_end_ns_idx
            ON spans (response_status_code, end_ns)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS spans_request_method_path_end_ns_idx
            ON spans (request_method, request_path, end_ns)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS span_events_name_time_ns_idx
            ON span_events (name, time_ns)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS span_events_span_id_time_ns_idx
            ON span_events (span_id, time_ns)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS span_links_target_span_id_idx
            ON span_links (target_span_id)
            """
        )

    tracer._prepare_database()
    with sqlite3.connect(db_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        assert indexes == {
            "spans_trace_id_start_ns_idx",
            "spans_end_ns_idx",
            "spans_error_end_ns_idx",
        }
        user_version = connection.execute("PRAGMA user_version").fetchone()
        assert user_version == (2,)


def test_sqlite_tracer_buffers_when_used_in_tracer_scope(tmp_path) -> None:
    db_path = tmp_path / "traces.sqlite3"

    with SqliteTracer(
        db_path,
        flush_interval=60.0,
        max_batch_spans=2,
    ) as tracer:
        with tracer.create("request") as span:
            span.attr("phase", "run")

        with sqlite3.connect(db_path) as connection:
            count = connection.execute("SELECT count(*) FROM spans").fetchone()
            assert count == (0,)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT name, attributes_json FROM spans"
        ).fetchone()
        assert row == ("request", '{"phase":"run"}')


def test_sqlite_tracer_records_dropped_spans_in_status_table(tmp_path) -> None:
    class SlowSqliteTracer(SqliteTracer):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.block_writer = threading.Event()
            self.writer_started = threading.Event()

        def _write_batch(self, connection, states) -> None:
            self.writer_started.set()
            self.block_writer.wait(timeout=1.0)
            return super()._write_batch(connection, states)

    db_path = tmp_path / "traces.sqlite3"
    tracer = SlowSqliteTracer(
        db_path,
        flush_interval=60.0,
        max_pending_spans=1,
        max_batch_spans=1,
    )

    with tracer:
        with tracer.create("one"):
            pass
        assert tracer.writer_started.wait(timeout=1.0)

        with tracer.create("two"):
            pass
        with tracer.create("three"):
            pass

        tracer.block_writer.set()

    assert tracer.dropped_spans == 1
    assert tracer.stats()["dropped_spans"] == 1

    with sqlite3.connect(db_path) as connection:
        status_row = connection.execute(
            """
            SELECT dropped_spans, writer_error_count, last_writer_error
            FROM telemetry_status
            """
        ).fetchone()
        assert status_row == (1, 0, None)


def test_sqlite_tracer_surfaces_writer_errors_in_status_table(tmp_path) -> None:
    class FlakySqliteTracer(SqliteTracer):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.failed_once = False

        def _write_batch(self, connection, states) -> None:
            if not self.failed_once:
                self.failed_once = True
                raise sqlite3.OperationalError("simulated write failure")
            return super()._write_batch(connection, states)

    db_path = tmp_path / "traces.sqlite3"
    tracer = FlakySqliteTracer(
        db_path,
        flush_interval=60.0,
        max_batch_spans=1,
    )

    with tracer:
        with tracer.create("request") as span:
            span.attr("phase", "run")

    assert tracer.writer_error_count == 1
    assert tracer.writer_error == "OperationalError: simulated write failure"

    with sqlite3.connect(db_path) as connection:
        span_row = connection.execute(
            "SELECT name, attributes_json FROM spans"
        ).fetchone()
        assert span_row == ("request", '{"phase":"run"}')

        status_row = connection.execute(
            """
            SELECT dropped_spans, writer_error_count, last_writer_error
            FROM telemetry_status
            """
        ).fetchone()
        assert status_row == (
            0,
            1,
            "OperationalError: simulated write failure",
        )
