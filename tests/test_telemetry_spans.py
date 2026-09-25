"""Tests for span primitives: `RecordingSpan`, `ProxySpan`, `NoOpSpan`."""

import json
import os
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest

from stario.telemetry.json import JsonTracer
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.spans import NoOpSpan, ProxySpan, RecordingSpan, _span_id


def make_tracer() -> tuple[JsonTracer, StringIO]:
    output = StringIO()
    return JsonTracer(output), output


@contextmanager
def tracing() -> Generator[tuple[JsonTracer, StringIO]]:
    tracer, output = make_tracer()
    with tracer:
        yield tracer, output


def emitted(output: StringIO) -> list[dict]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


class TestRecordingSpanLifecycle:
    def test_start_end_emits_span(self):
        with tracing() as (tracer, output):
            span = tracer.create("work")
            span.start()
            span.end()

        (payload,) = emitted(output)
        assert payload["name"] == "work"
        assert payload["status"] == "ok"
        assert payload["duration_ns"] >= 0

    def test_end_without_start_raises(self):
        with tracing() as (tracer, _):
            span = tracer.create("work")
            with pytest.raises(RuntimeError, match="never started"):
                span.end()

    def test_double_end_is_silent_noop_and_emits_once(self):
        with tracing() as (tracer, output):
            span = tracer.create("work")
            span.start()
            span.end()
            span.end()

        assert len(emitted(output)) == 1

    def test_finished_span_ignores_late_recording(self):
        with tracing() as (tracer, output):
            span = tracer.create("work")
            span.start()
            span.attr("kept", True)
            span.end()

            span.attr("late", True)
            span.attrs({"also.late": True})
            span["setitem.late"] = True
            span.event("late")
            span.exception(RuntimeError("late"))
            span.link("late", uuid4())
            span.fail("late")

        (payload,) = emitted(output)
        assert payload["status"] == "ok"
        assert payload["attributes"] == {"kept": True}
        assert "events" not in payload
        assert "links" not in payload


class TestRecordingSpanErrors:
    def test_fail_marks_error_status(self):
        with tracing() as (tracer, output):
            span = tracer.create("work")
            span.start()
            span.fail("it broke")
            span.end()

        (payload,) = emitted(output)
        assert payload["status"] == "error"
        assert payload["error"] == "it broke"

    def test_context_manager_exception_records_error_and_event(self):
        tracer, output = make_tracer()
        with (
            pytest.raises(ValueError, match="bad input"),
            tracer,
            tracer.create("work"),
        ):
            raise ValueError("bad input")

        (payload,) = emitted(output)
        assert payload["status"] == "error"
        assert payload["error"] == "bad input"

        (event,) = payload["events"]
        assert event["name"] == "exception"
        assert event["attributes"]["exc.type"] == "ValueError"
        assert event["attributes"]["exc.message"] == "bad input"
        assert "Traceback" in event["body"]


class TestRecordingSpanAttributes:
    def test_attr_attrs_and_setitem_merge(self):
        with tracing() as (tracer, output), tracer.create("work") as span:
            span.attr("a", 1)
            span.attrs({"b": 2, "a": 3})
            span["c"] = 4
            span.attrs({})  # empty merge is a no-op

        (payload,) = emitted(output)
        assert payload["attributes"] == {"a": 3, "b": 2, "c": 4}

    def test_attrs_copy_is_defensive(self):
        source = {"a": 1}
        with tracing() as (tracer, output), tracer.create("work") as span:
            span.attrs(source)
            source["a"] = 999

        (payload,) = emitted(output)
        assert payload["attributes"] == {"a": 1}


class TestSpanParenting:
    def test_step_creates_child_span(self):
        with (
            tracing() as (tracer, output),
            tracer.create("root") as root,
            root.step("child"),
        ):
            pass

        child, parent = emitted(output)
        assert child["name"] == "child"
        assert child["parent_id"] == parent["span_id"]
        assert child["trace_id"] == parent["trace_id"]

    def test_new_trace_makes_a_new_root_span(self):
        # `span.new_trace` intentionally starts a NEW trace (no parent),
        # unlike `span.step` which creates a child.
        with (
            tracing() as (tracer, output),
            tracer.create("root") as root,
            root.new_trace("sibling"),
        ):
            pass

        sibling, root_payload = emitted(output)
        assert "parent_id" not in sibling
        assert sibling["trace_id"] != root_payload["trace_id"]

    def test_noop_span_returns_singleton_for_step_and_new_trace(self):
        tracer = NoOpTracer()
        root = tracer.create("root")
        assert isinstance(root, NoOpSpan)

        child = root.step("child")
        assert child is root

        sibling = root.new_trace("sibling")
        assert sibling is root


class TestProxySpan:
    def test_replace_redirects_all_calls_to_new_span(self):
        with tracing() as (tracer, output):
            first = tracer.create("first")
            proxy = ProxySpan(first)
            proxy.start()
            first_id = proxy.id
            proxy.end()

            second = tracer.create("second")
            proxy.replace(second)
            assert proxy.id == second.id != first_id

            proxy.attr("phase", "two")
            proxy.start()
            proxy.link("previous", first_id)
            proxy.end()

        first_payload, second_payload = emitted(output)
        assert first_payload["name"] == "first"
        assert second_payload["name"] == "second"
        assert second_payload["attributes"] == {"phase": "two"}
        assert second_payload["links"] == [
            {"name": "previous", "span_id": first_id and str(first_id)}
        ]


class TestSpanId:
    def test_is_uuid_version_7_rfc4122(self) -> None:
        value = _span_id()
        assert value.version == 7
        assert (value.int >> 62) & 0b11 == 0b10

    def test_is_unique(self) -> None:
        seen = {_span_id() for _ in range(256)}
        assert len(seen) == 256

    def test_is_monotonic_in_the_same_millisecond(self) -> None:
        values = [_span_id() for _ in range(32)]
        assert values == sorted(values)

    def test_create_assigns_root_and_child_ids(self) -> None:
        with tracing() as (tracer, _output):
            root = RecordingSpan.create(tracer, "root")
            child = RecordingSpan.create(tracer, "child", parent=root)
        assert root.id.version == 7
        assert root.trace_id == root.id
        assert root.parent_id is None
        assert child.trace_id == root.trace_id
        assert child.parent_id == root.id
        assert child.id != root.id

    @pytest.mark.skipif(sys.version_info < (3, 14), reason="stdlib uuid7 is 3.14+")
    def test_matches_stdlib_uuid7(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import uuid as stdlib_uuid

        from stario.telemetry import spans

        std: Any = stdlib_uuid
        saved = (
            std._last_timestamp_v7,
            std._last_counter_v7,
            spans._last_timestamp_v7,
            spans._last_counter_v7,
        )
        blobs = [bytes(range(10)), bytes(range(4)), bytes(range(4, 8))]
        index = {"n": 0}

        def fake_urandom(n: int) -> bytes:
            blob = blobs[index["n"]]
            index["n"] += 1
            assert len(blob) == n
            return blob

        monkeypatch.setattr(time, "time_ns", lambda: 1_700_000_000_000_000_000)
        monkeypatch.setattr(os, "urandom", fake_urandom)

        try:
            std._last_timestamp_v7 = None
            std._last_counter_v7 = 0
            spans._last_timestamp_v7 = None
            spans._last_counter_v7 = 0
            theirs = [std.uuid7() for _ in range(3)]
            std._last_timestamp_v7 = None
            std._last_counter_v7 = 0
            spans._last_timestamp_v7 = None
            spans._last_counter_v7 = 0
            index["n"] = 0
            ours = [spans._span_id() for _ in range(3)]
            assert ours == theirs
        finally:
            std._last_timestamp_v7 = saved[0]
            std._last_counter_v7 = saved[1]
            spans._last_timestamp_v7 = saved[2]
            spans._last_counter_v7 = saved[3]
