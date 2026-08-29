"""Handler start/finish: drive until the first await, log failures, abort if nothing was sent.

Does not map exceptions to HTTP and does not call `Writer.end()`.
"""

from __future__ import annotations

import asyncio
import logging

from stario.http.context import Context
from stario.http.writer import Writer
from stario.telemetry.spans import NoOpSpan

_log = logging.getLogger("stario.http")


def finish_handler(c: Context, w: Writer, exc=None, cancelled: bool = False) -> None:
    """Log/abort after the handler returns, fails, or is cancelled. Close the span."""
    span = c.span
    record = type(span) is not NoOpSpan

    if cancelled:
        if not w.completed:
            w.abort()
    elif exc is not None:
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


def on_handler_done(c: Context, w: Writer, task) -> None:
    """Run after the handler task finishes (success, failure, or cancel)."""
    if not task.done():
        return
    if task.cancelled():
        finish_handler(c, w, cancelled=True)
        return
    finish_handler(c, w, exc=task.exception())


async def resume_started(coro, pending):
    """Continue a coroutine after an initial ``send(None)`` that yielded `pending`."""
    try:
        while True:
            try:
                value = await pending if pending is not None else None
            except asyncio.CancelledError:
                try:
                    pending = coro.throw(asyncio.CancelledError())
                except StopIteration as e:
                    return e.value
                continue
            except BaseException as err:
                try:
                    pending = coro.throw(err)
                except StopIteration as e:
                    return e.value
                continue
            try:
                pending = coro.send(value)
            except StopIteration as e:
                return e.value
    except asyncio.CancelledError:
        coro.close()
        raise


def start_handler(c: Context, w: Writer, handler, *, loop, create_task, eager_start: bool = True):
    """Run `handler` until it suspends. Returns a Task, or None if it finished inline."""
    coro = handler(c, w)
    try:
        pending = coro.send(None)
    except StopIteration:
        finish_handler(c, w)
        return None
    except Exception as exc:
        finish_handler(c, w, exc=exc)
        if not w.started:
            raise
        return None
    task = create_task(resume_started(coro, pending), loop=loop, eager_start=False)
    if task.done():
        on_handler_done(c, w, task)
        return None
    task.add_done_callback(lambda done: on_handler_done(c, w, done))
    return task
