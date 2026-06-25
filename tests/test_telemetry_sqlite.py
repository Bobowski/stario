"""Tests for SqliteTracer persistence."""

import json
import sqlite3
from uuid import uuid4

from stario.telemetry.sqlite import SqliteTracer


def test_sqlite_tracer_context_manager_persists_span_tree(tmp_path) -> None:
    db_path = tmp_path / "traces.sqlite3"
    tracer = SqliteTracer(db_path)

    target_id = uuid4()
    with tracer, tracer.create("request") as span:
        span.attr("request.method", "GET")
        span.attr("request.path", "/")
        with span.step("router.dispatch") as child:
            child.attr("route.name", "home")
        span.event("handler.hit", {"path": "/"}, body="handler returned ok")
        span.link("related.request", target_id, {"kind": "related"})
        span.attr("response.status_code", 200)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                hex(span_id),
                hex(trace_id),
                hex(parent_id),
                name,
                status,
                error,
                attrs_json
            FROM spans
            ORDER BY parent_id IS NOT NULL, start_ns
            """
        ).fetchall()
        assert len(rows) == 2

        root_row, child_row = rows
        assert root_row[3] == "request"
        assert root_row[4] == "ok"
        assert root_row[5] is None
        assert json.loads(root_row[6]) == {
            "request.method": "GET",
            "request.path": "/",
            "response.status_code": 200,
        }
        assert root_row[2] == ""

        assert child_row[3] == "router.dispatch"
        assert child_row[1] == root_row[0]
        assert child_row[2] == root_row[0]
        assert json.loads(child_row[6]) == {"route.name": "home"}

        event_row = connection.execute(
            """
            SELECT name, attrs_json, body
            FROM span_events
            WHERE span_id = (
                SELECT span_id FROM spans WHERE parent_id IS NULL
            )
            """
        ).fetchone()
        assert event_row == (
            "handler.hit",
            '{"path":"/"}',
            "handler returned ok",
        )

        link_row = connection.execute(
            """
            SELECT hex(target_span_id), name, attrs_json
            FROM span_links
            WHERE span_id = (
                SELECT span_id FROM spans WHERE parent_id IS NULL
            )
            """
        ).fetchone()
        assert link_row[0].lower() == target_id.hex
        assert link_row[1] == "related.request"
        assert link_row[2] == '{"kind":"related"}'
