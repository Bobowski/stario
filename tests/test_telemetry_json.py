import io
import json

import pytest

from stario.telemetry.json import JsonTracer


def test_json_tracer_create_returns_stopped_span() -> None:
    tracer = JsonTracer(output=io.StringIO())

    span = tracer.create("request")

    assert tracer._spans[span.id].start_ns == 0


def test_json_tracer_end_requires_explicit_start() -> None:
    tracer = JsonTracer(output=io.StringIO())
    span = tracer.create("request")

    with pytest.raises(RuntimeError, match="never started"):
        span.end()


def test_json_tracer_context_manager_starts_and_flushes_span() -> None:
    output = io.StringIO()
    tracer = JsonTracer(output=output)

    with tracer.create("request") as span:
        span.attr("phase", "run")

    payload = json.loads(output.getvalue())
    assert payload["name"] == "request"
    assert payload["attributes"] == {"phase": "run"}
    assert payload["start_ns"] > 0
    assert payload["duration_ns"] is not None


def test_json_tracer_buffers_when_used_in_tracer_scope() -> None:
    output = io.StringIO()

    with JsonTracer(
        output=output,
        flush_interval=60.0,
        max_batch_spans=2,
    ) as tracer:
        with tracer.create("request") as span:
            span.attr("phase", "run")

        assert output.getvalue() == ""

    payload = json.loads(output.getvalue())
    assert payload["name"] == "request"
    assert payload["attributes"] == {"phase": "run"}
