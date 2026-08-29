"""Handler-task finish: log failures, abort if nothing was sent, close the span.

Does not map exceptions to HTTP and does not call `Writer.end()`.
"""

from __future__ import annotations

import logging

from stario.http.context import Context
from stario.http.writer import Writer
from stario.telemetry.spans import NoOpSpan

_log = logging.getLogger("stario.http")


def on_handler_done(c: Context, w: Writer, task) -> None:
    """Run after the handler task finishes (success, failure, or cancel)."""
    if not task.done():
        return
    span = c.span
    record = type(span) is not NoOpSpan

    if task.cancelled():
        if not w.completed:
            w.abort()
    else:
        exc = task.exception()
        if exc is not None:
            _log.error("Handler failed", exc_info=exc)
            if record:
                span.fail(str(exc))
                span.exception(exc)
            if not w.completed:
                w.abort()
        elif not w.completed:
            _log.error(
                "Handler returned without sending a response (%s %s)",
                c.req.method,
                c.req.path,
            )
            if record:
                span.fail("Handler returned without sending a response")
            w.abort()

    if record:
        span.attr("response.status_code", w.status_code)
        span.end()
