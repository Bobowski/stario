"""TTY-style, one-line span output for prefixed or multiplexed consoles."""

import sys
from typing import Any, TextIO

from stario._terminal import enable_vt_for_stream

from .buffered import _BufferedTracer
from .presentation import (
    format_duration,
    format_timestamp,
    short_id,
    span_status_style,
    styled,
)
from .spans import RecordedEvent, RecordedLink, RecordingSpan

_DEFAULT_FLUSH_INTERVAL = 0.125
_DEFAULT_MAX_PENDING_SPANS = 65536
_DEFAULT_MAX_BATCH_SPANS = 512

_CONTROL_ESCAPES = {
    **{code: f"\\u{code:04x}" for code in range(0x20)},
    **{code: f"\\u{code:04x}" for code in range(0x7F, 0xA0)},
    0x08: r"\b",
    0x09: r"\t",
    0x0A: r"\n",
    0x0C: r"\f",
    0x0D: r"\r",
    0x2028: r"\u2028",
    0x2029: r"\u2029",
}


def _safe_text(value: Any) -> str:
    text = str(value).translate(_CONTROL_ESCAPES)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _field(name: str, value: Any, separator: str = ": ") -> str:
    return styled(f"{_safe_text(name)}{separator}", "dim") + styled(
        _safe_text(value), "white"
    )


def _span_trailer(span: RecordingSpan, duration_ns: int) -> str:
    trailer = f"{format_duration(duration_ns)} {short_id(span.id)}"
    if not span.failed:
        return trailer
    return f"[{_safe_text(span.error or 'failed')}] {trailer}"


def _format_link(link: RecordedLink) -> str:
    text = (
        styled("link ", "dim")
        + styled(_safe_text(link.name), "white")
        + styled(f" {short_id(link.span_id)}", "dim")
    )
    if link.attributes:
        attributes = " ".join(
            _field(key, value, "=") for key, value in link.attributes.items()
        )
        text += f"  {attributes}"
    return text


def _format_event(event: RecordedEvent, start_ns: int) -> str:
    style = "red" if event.name == "exception" else "white"
    text = styled(f"+{format_duration(event.time_ns - start_ns)} ", "dim") + styled(
        _safe_text(event.name), style
    )
    if event.attributes:
        attributes = " ".join(
            _field(key, value) for key, value in event.attributes.items()
        )
        text += f"  {attributes}"
    if event.body is not None:
        text += "  " + styled(_safe_text(event.body), "dim")
    return text


def _format_span_line(span: RecordingSpan) -> str:
    if span.start_ns is None or span.end_ns is None:
        raise RuntimeError("ConsoleTracer can only format finished spans")

    fields = [
        styled(format_timestamp(span.start_ns), "dim"),
        styled(_safe_text(span.name), "white"),
        styled(
            _span_trailer(span, span.end_ns - span.start_ns),
            span_status_style(span),
        ),
    ]
    if span.attributes:
        fields.extend(
            _field(key, span.attributes[key]) for key in sorted(span.attributes)
        )
    for link in span.links or []:
        fields.append(_format_link(link))
    for event in sorted(span.events or [], key=lambda event: event.time_ns):
        fields.append(_format_event(event, span.start_ns))
    return "  ".join(fields)


class ConsoleTracer(_BufferedTracer):
    """Write each finished span as one TTY-style line, without repaint controls."""

    __slots__ = ("_output",)

    def __init__(self, output: TextIO | None = None) -> None:
        super().__init__(
            tracer_label="console tracer",
            tracer_type_name="ConsoleTracer",
            flush_interval=_DEFAULT_FLUSH_INTERVAL,
            max_pending_spans=_DEFAULT_MAX_PENDING_SPANS,
            max_batch_spans=_DEFAULT_MAX_BATCH_SPANS,
        )
        self._output = output if output is not None else sys.stdout

    def __enter__(self) -> ConsoleTracer:
        enable_vt_for_stream(self._output)
        super().__enter__()
        return self

    def _thread_name(self) -> str:
        return "stario-console-tracer"

    def _write_batch(self, spans: list[RecordingSpan]) -> None:
        lines: list[str] = []
        for span in spans:
            try:
                lines.append(_format_span_line(span))
            except Exception as exc:
                self._record_serialization_error()
                self._emit_stderr(
                    "console tracer formatting error; span omitted: "
                    f"{type(exc).__name__}"
                )

        if lines:
            self._output.write("\n".join(lines) + "\n")
            self._output.flush()
