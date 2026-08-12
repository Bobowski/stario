"""Shared visual primitives for human-readable telemetry output."""

from datetime import datetime
from uuid import UUID

from stario._terminal import RESET, SGR, color_enabled

from .spans import RecordingSpan


def styled(text: str, style: str) -> str:
    if not color_enabled():
        return text
    prefix = SGR.get(style)
    if not prefix:
        return text
    return f"{prefix}{text}{RESET}"


def format_timestamp(timestamp_ns: int) -> str:
    return datetime.fromtimestamp(timestamp_ns / 1e9).strftime("%H:%M:%S.%f")[:-3]


def format_duration(duration_ns: int) -> str:
    if duration_ns < 1_000_000:
        microseconds = duration_ns / 1e3
        if microseconds < 10:
            return f"{microseconds:.1f} us"
        return f"{microseconds:.0f} us"

    milliseconds = duration_ns / 1e6
    if milliseconds < 10:
        return f"{milliseconds:.1f} ms"
    if milliseconds < 1000:
        return f"{milliseconds:.0f} ms"
    if milliseconds < 60_000:
        return f"{milliseconds / 1000:.2f} s"
    minutes, seconds = divmod(int(milliseconds / 1000), 60)
    if minutes < 60:
        return f"{minutes}:{seconds:02d} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def short_id(value: UUID) -> str:
    text = str(value)
    return text[-8:] if len(text) >= 8 else text


def span_status_style(span: RecordingSpan) -> str:
    if span.in_progress:
        return "cyan"
    if span.failed:
        return "red"
    for key in ("response.status_code", "status_code"):
        if not span.attributes or key not in span.attributes:
            continue
        try:
            code = int(span.attributes[key])
        except OverflowError, TypeError, ValueError:
            continue
        if 200 <= code < 300:
            return "green"
        if 300 <= code < 500:
            return "yellow"
        return "red"
    return "green"
