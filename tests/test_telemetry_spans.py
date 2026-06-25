"""Tests for span primitives: `RecordingSpan`, `ProxySpan`, `NoOpSpan`."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from io import StringIO
from uuid import uuid4

import pytest

from stario.telemetry.json import JsonTracer
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.spans import NoOpSpan, ProxySpan


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
