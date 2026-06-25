"""Shared formatting and JSON encoding for telemetry sinks.

Tracebacks hide Stario framework frames so application errors stay readable.
Span attributes are encoded with `dumps_json` (`default=str` for unknown
types); event bodies are stricter — see `serialize_event_body`.
"""

import json
from functools import lru_cache
from pathlib import Path
from traceback import StackSummary, format_exception_only, format_list
from types import TracebackType
from typing import Any

from .core import EventBody

_JSON_ENCODER = json.JSONEncoder(
    default=str,
    separators=(",", ":"),
    ensure_ascii=False,
)


@lru_cache(maxsize=1)
def _stario_install_root() -> Path:
    import stario

    return Path(stario.__file__).resolve().parent


@lru_cache(maxsize=4096)
def _should_hide_frame(filename: str) -> bool:
    if filename.startswith("<"):
        return True
    try:
        path = Path(filename).resolve()
    except OSError:
        return False
    try:
        if path.is_relative_to(_stario_install_root()):
            return True
    except ValueError:
        pass
    return False


def _walk_tb_application_frames(tb: TracebackType | None):
    while tb is not None:
        fn = tb.tb_frame.f_code.co_filename
        if not _should_hide_frame(fn):
            yield tb.tb_frame, tb.tb_lineno
        tb = tb.tb_next


def format_exception_for_telemetry(exc: BaseException) -> str:
    """Format `exc` while hiding Stario install frames from the traceback."""
    tb = exc.__traceback__
    if tb is None:
        return "".join(format_exception_only(type(exc), exc))
    w = tb
    while w is not None:
        if not _should_hide_frame(w.tb_frame.f_code.co_filename):
            break
        w = w.tb_next
    else:
        return "".join(format_exception_only(type(exc), exc))
    stack = StackSummary.extract(_walk_tb_application_frames(tb))
    parts: list[str] = []
    if stack:
        parts.append("Traceback (most recent call last):\n")
        parts.extend(format_list(stack))
    parts.extend(format_exception_only(type(exc), exc))
    return "".join(parts)


def dumps_json(value: Any) -> str:
    """Compact JSON for span export; unknown value types become strings."""
    return _JSON_ENCODER.encode(value)


def serialize_event_body(body: EventBody) -> str | None:
    """Normalize an event body for storage.

    Event attributes carry structured data. Bodies are for human-readable
    diagnostics and tracebacks — arbitrary objects are rejected instead of
    being implicitly stringified.
    """
    if body is None:
        return None
    value: object = body
    if isinstance(value, BaseException):
        return format_exception_for_telemetry(value)
    if type(value) is str:
        return value
    raise TypeError(
        "Telemetry event body must be str, BaseException, or None; "
        "put structured data in attributes."
    )


def serialize_json(value: Any) -> str | None:
    """JSON-encode a value for SQLite columns; `None` stays `None`."""
    if value is None:
        return None
    return dumps_json(value)
