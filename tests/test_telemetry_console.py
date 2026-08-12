import io
import os
import re
from datetime import datetime
from typing import Any, cast
from uuid import uuid4, uuid7

import pytest

from stario._terminal import RESET, SGR
from stario.cli.help import SERVER_ENV_EPILOG
from stario.telemetry import TelemetryStats
from stario.telemetry.console import ConsoleTracer, _format_span_line
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.spans import RecordingSpan
from stario.telemetry.tty import TTYRenderer

_T0 = 1_700_000_000 * 1_000_000_000
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain(text: str) -> str:
    return _SGR_RE.sub("", text)


class BrokenOutput(io.StringIO):
    def write(self, value: str) -> int:
        raise OSError("closed console")


def test_cli_help_documents_console_tracer() -> None:
    assert (
        "STARIO_TRACER=auto|console|tty|json|noop|sqlite|module:callable"
        in SERVER_ENV_EPILOG
    )
    assert (
        "console — one TTY-style line per finished span (runner-safe)"
        in SERVER_ENV_EPILOG
    )


class HostileStringError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


class BadString:
    def __str__(self) -> str:
        raise HostileStringError()


def test_console_flattens_tty_presentation_into_one_exact_line(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    times = iter(
        (
            _T0 - 1,
            _T0,
            _T0 + 3_000_000,
            _T0 + 1_000_000,
            _T0 + 5_000_000,
        )
    )
    monkeypatch.setattr("stario.telemetry.spans.time.time_ns", lambda: next(times))
    output = io.StringIO()
    first_target = uuid4()
    second_target = uuid4()

    with (
        ConsoleTracer(output=output) as tracer,
        tracer.create("request", {"z": "last", "a": True}) as span,
    ):
        span.event("later", {"phase": "run", "attempt": 2}, body="body\nline")
        span.event("earlier", {"second": 2, "first": 1})
        span.link("first", first_target, {"kind": "retry", "attempt": 2})
        span.link("second", second_target, {"source": "cache", "fresh": True})

    timestamp = datetime.fromtimestamp(_T0 / 1e9).strftime("%H:%M:%S.%f")[:-3]
    assert output.getvalue() == (
        f"{timestamp}  request  5.0 ms {str(span.id)[-8:]}"
        f"  a: True  z: last"
        f"  link first {str(first_target)[-8:]}  kind=retry attempt=2"
        f"  link second {str(second_target)[-8:]}  source=cache fresh=True"
        "  +1.0 ms earlier  second: 2 first: 1"
        "  +3.0 ms later  phase: run attempt: 2  body\\nline\n"
    )


def test_console_emits_finished_spans_in_finish_order_and_drains_on_exit(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = io.StringIO()

    with ConsoleTracer(output=output) as tracer:
        root = tracer.create("root")
        root.start()
        child = root.step("child")
        child.start()
        child.end()
        never_started = tracer.create("never-started")
        root.end()

    lines = output.getvalue().splitlines()
    assert len(lines) == 2
    assert "child" in lines[0]
    assert "root" in lines[1]
    assert "never-started" not in output.getvalue()
    assert never_started.started is False


@pytest.mark.parametrize(
    ("attributes", "style"),
    [
        ({"response.status_code": 200}, "green"),
        ({"response.status_code": 404}, "yellow"),
        ({"response.status_code": 500}, "red"),
        ({"response.status_code": float("inf")}, "green"),
    ],
)
def test_console_header_colors_match_tty(
    monkeypatch, attributes: dict[str, int | float], style: str
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    times = iter((_T0 - 1, _T0, _T0 + 5_000_000))
    monkeypatch.setattr("stario.telemetry.spans.time.time_ns", lambda: next(times))
    output = io.StringIO()

    with ConsoleTracer(output=output) as tracer:
        span = tracer.create("request", attributes)
        with span:
            pass

    trailer = f"5.0 ms {str(span.id)[-8:]}"
    tty_text = TTYRenderer(120, {}).root_block(span)
    console_text = output.getvalue()
    for text in (console_text, tty_text):
        assert f"{SGR['white']}request{RESET}" in text
        assert f"{SGR[style]}{trailer}{RESET}" in text
    assert f"response.status_code: {attributes['response.status_code']}" in _plain(
        console_text
    )
    assert tracer.stats() == TelemetryStats()


def test_console_failure_color_and_no_color(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = io.StringIO()

    with ConsoleTracer(output=output) as tracer, tracer.create("request") as span:
        span.fail("unavailable")

    assert f"{SGR['red']}[unavailable]" in output.getvalue()

    monkeypatch.setenv("NO_COLOR", "")
    plain_output = io.StringIO()
    with ConsoleTracer(output=plain_output) as tracer, tracer.create("plain"):
        pass
    assert "\x1b" not in plain_output.getvalue()


def test_console_escapes_all_user_text_and_lone_surrogates(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
    target = uuid4()

    with (
        ConsoleTracer(output=output) as tracer,
        tracer.create("span\n\x1b[2J\ud800") as span,
    ):
        span.attr("key\r\x1b[2K", "value\n\u0085\u2028")
        span.link("link\n\x1b[1A", target, {"kind\n": "bad\x1b[2J"})
        span.event(
            "event\n\x1b[2J",
            {"phase\n": "bad\u2029"},
            body="body\r\n\x1b[2J",
        )
        span.fail("failure\n\x1b[2J")

    text = buffer.getvalue().decode("utf-8")
    assert len(text.splitlines()) == 1
    assert text.count("\n") == 1
    assert "\r" not in text
    generated = _ANSI_RE.findall(text)
    assert generated
    assert set(generated) <= set(SGR.values())
    plain = _plain(text)
    assert "\x1b" not in plain
    for escaped in (
        r"span\n\u001b[2J\ud800",
        r"key\r\u001b[2K",
        r"value\n\u0085\u2028",
        r"link\n\u001b[1A",
        r"event\n\u001b[2J",
        r"bad\u2029",
        r"body\r\n\u001b[2J",
        r"failure\n\u001b[2J",
    ):
        assert escaped in plain


@pytest.mark.parametrize(
    ("start_ns", "end_ns"),
    [(None, _T0), (_T0, None)],
)
def test_console_rejects_malformed_timing_without_partial_output(
    start_ns: int | None,
    end_ns: int | None,
) -> None:
    span_id = uuid7()
    span = RecordingSpan(
        span_id,
        NoOpTracer(),
        span_id,
        None,
        "malformed",
        start_ns=start_ns,
        end_ns=end_ns,
    )

    with pytest.raises(RuntimeError, match="finished spans"):
        _format_span_line(span)


def test_console_formatting_failure_omits_only_bad_span(capsys, monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = io.StringIO()
    tracer = ConsoleTracer(output=output)

    with tracer:
        with tracer.create(cast(Any, BadString())):
            pass
        with tracer.create("good"):
            pass

    assert "good" in output.getvalue()
    assert tracer.stats() == TelemetryStats(serialization_error_count=1)
    diagnostic = capsys.readouterr().err
    assert len(diagnostic.splitlines()) == 1
    assert "console tracer formatting error; span omitted: HostileStringError" in (
        diagnostic
    )


def test_console_writer_failure_isolated(capsys) -> None:
    tracer = ConsoleTracer(output=BrokenOutput())

    with tracer, tracer.create("request"):
        pass

    assert "console tracer write error" in capsys.readouterr().err
    stats = tracer.stats()
    assert stats == TelemetryStats(
        writer_error_count=1,
        last_writer_error="OSError: closed console",
        last_writer_error_at_ns=stats.last_writer_error_at_ns,
    )


def test_console_colors_survive_real_pipe_with_one_prefix(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    read_fd, write_fd = os.pipe()
    output = os.fdopen(write_fd, "w", encoding="utf-8")

    assert output.isatty() is False
    with ConsoleTracer(output=output) as tracer, tracer.create("request"):
        pass
    output.close()
    with os.fdopen(read_fd, encoding="utf-8") as reader:
        text = reader.read()

    assert SGR["green"] in text
    prefixed = "".join(f"web | {line}\n" for line in text.splitlines())
    assert prefixed.count("web | ") == 1
    assert len(prefixed.splitlines()) == 1
