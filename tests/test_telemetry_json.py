import io
import json

import pytest

from stario.telemetry import TelemetryStats
from stario.telemetry.json import JsonTracer


class BrokenWriteOutput(io.StringIO):
    def write(self, s: str) -> int:
        raise OSError("closed collector")


def test_json_tracer_buffers_and_flushes_on_exit() -> None:
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


def test_json_tracer_writes_ndjson_file_and_reopens(tmp_path) -> None:
    path = tmp_path / "sub" / "spans.ndjson"
    tracer = JsonTracer(path=path, flush_interval=60.0)

    with tracer:
        with tracer.create("one"):
            pass
        with tracer.create("two"):
            pass

    assert tracer._output.closed
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["name"] for line in lines] == ["one", "two"]

    with tracer:
        with tracer.create("three"):
            pass
        with tracer.create("four"):
            pass

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["name"] for line in lines] == ["one", "two", "three", "four"]


def test_json_tracer_rejects_output_and_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="output or path, not both"):
        JsonTracer(output=io.StringIO(), path=tmp_path / "spans.ndjson")


def test_json_tracer_counts_background_write_errors(capsys) -> None:
    tracer = JsonTracer(
        output=BrokenWriteOutput(), flush_interval=60.0, max_batch_spans=1
    )

    with tracer, tracer.create("request"):
        pass

    assert "json tracer write error" in capsys.readouterr().err
    stats = tracer.stats()
    assert stats == TelemetryStats(
        writer_error_count=1,
        last_writer_error="OSError: closed collector",
        last_writer_error_at_ns=stats.last_writer_error_at_ns,
    )


def test_json_tracer_skips_non_serializable_span(capsys) -> None:
    output = io.StringIO()
    tracer = JsonTracer(
        output=output,
        flush_interval=60.0,
        max_batch_spans=10,
    )

    cyclical: dict[str, object] = {}
    cyclical["self"] = cyclical

    with tracer:
        with tracer.create("bad") as span:
            span.attr("key", cyclical)
        with tracer.create("good") as span:
            span.attr("ok", True)

    assert tracer.stats().serialization_error_count == 1
    assert "json tracer serialization error" in capsys.readouterr().err
    lines = [
        json.loads(line) for line in output.getvalue().splitlines() if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["name"] == "good"
