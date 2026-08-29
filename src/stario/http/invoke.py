"""
Per-request schedule: resolve a handler, run it as a task, finish the writer.

The protocol calls `schedule_request` instead of `App.__call__`. Routing is
`find_handler` (plus an explicit trailing-slash 308). Errors are three built-in
types — no registry, no MRO walk of user exceptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import stario.responses as responses
from stario.exceptions import (
    ClientDisconnected,
    HttpException,
    RedirectException,
    StarioRuntime,
)
from stario.http.context import Context, Handler
from stario.http.writer import Writer
from stario.routing.locations import normalize_path
from stario.telemetry.spans import NoOpSpan

type DoneCallback = Callable[[asyncio.Task[None]], None]


async def trailing_slash_redirect(c: Context, w: Writer) -> None:
    """308 to the canonical path (leading `/`, no trailing slash). Query kept."""
    target = normalize_path(c.req.path)
    if c.req.query_bytes:
        target = f"{target}?{c.req.query_bytes.decode('latin-1')}"
    responses.redirect(w, target, 308)


def resolve_handler(app: Any, c: Context) -> Handler:
    """Return the handler for this request. Trailing slashes do not hit the trie."""
    path = c.req.path
    if path != "/" and path.endswith("/"):
        return trailing_slash_redirect
    host = c.req.host if app.host_routing else ""
    handler, c.route = app.find_handler(host, path, c.req.method)
    return handler


def apply_failure(_c: Context, w: Writer, exc: BaseException) -> bool:
    """Write the explicit built-in outcome, or 500.

    Returns True when telemetry should record a failure: headers already
    sent, an unsafe redirect, or any exception that is not one of
    `HttpException`, `RedirectException`, or `ClientDisconnected`.
    """
    if w.started:
        return True
    try:
        if isinstance(exc, HttpException):
            responses.text(w, exc.detail or "Error", exc.status_code)
            return False
        if isinstance(exc, RedirectException):
            responses.redirect(w, exc.location, exc.status_code)
            return False
        if isinstance(exc, ClientDisconnected):
            w.abort()
            return False
    except Exception:
        if w.started:
            return True
        responses.text(w, "Internal Server Error", 500)
        return True
    responses.text(w, "Internal Server Error", 500)
    return True


def finish_scheduled(c: Context, w: Writer, task: asyncio.Task[None]) -> None:
    """Apply handler outcome, guarantee `end`/`abort`, close the request span."""
    span = c.span
    record = type(span) is not NoOpSpan
    fail_span = False
    exc: BaseException | None = None

    if task.cancelled():
        if not w.completed:
            w.end()
    else:
        exc = task.exception()
        if exc is None and not w.started and not w.completed:
            exc = StarioRuntime(
                "Handler returned without sending a response",
                context={
                    "method": c.req.method,
                    "path": c.req.path,
                    "route": c.route.pattern or None,
                },
                help_text=(
                    "Call a response helper such as responses.text/json/html/empty, "
                    "or explicitly use Writer.write_headers()/write()/end()."
                ),
            )
        if exc is not None:
            fail_span = apply_failure(c, w, exc)
            if not w.completed:
                if w.started:
                    w.abort()
                else:
                    w.end()
        elif not w.completed:
            w.end()

    if record:
        if fail_span and exc is not None:
            span.fail(str(exc))
            span.exception(exc)
        span.attr("response.status_code", w.status_code)
        span.end()


def schedule_request(
    app: Any,
    c: Context,
    w: Writer,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    eager_start: bool = False,
    on_done: DoneCallback | None = None,
) -> asyncio.Task[None]:
    """Find the handler and run `handler(c, w)` as a tracked task.

    Finish (errors, missing response, `writer.end()`, span) runs in a done
    callback — there is no wrapper coroutine around the handler.
    """
    span = c.span
    if type(span) is not NoOpSpan:
        span.start()
        span.attrs({"request.method": c.req.method, "request.path": c.req.path})

    handler = resolve_handler(app, c)
    task = app.create_task(
        handler(c, w),
        loop=loop,
        eager_start=eager_start,
    )

    def _done(done: asyncio.Task[None], /) -> None:
        finish_scheduled(c, w, done)
        if on_done is not None:
            on_done(done)

    if task.done():
        _done(task)
    else:
        task.add_done_callback(_done)
    return task
