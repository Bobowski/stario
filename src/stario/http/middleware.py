"""Middleware helpers for handler wrappers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import stario.responses as responses
from stario.exceptions import RequestBodyError, StarioError
from stario.http.context import Context, Handler, Middleware
from stario.http.writer import Writer

type ErrorResponder = Callable[[Context, Writer, BaseException], Awaitable[None]]


def catch_errors(
    *exc_types: type[BaseException],
    respond: ErrorResponder,
) -> Middleware:
    """Wrap a handler so listed exceptions become HTTP responses when nothing was sent.

    If the handler already started a response (`w.started`), the exception is
    re-raised so the framework can abort the in-flight body.
    """
    if not exc_types:
        raise StarioError(
            "catch_errors requires at least one exception type",
            help_text="Pass one or more exception classes, e.g. catch_errors(MyError, respond=...).",
        )

    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            try:
                await handler(c, w)
            except exc_types as exc:
                if w.started:
                    raise
                await respond(c, w, exc)

        return wrapped

    return middleware


async def respond_request_body_error(
    _c: Context,
    w: Writer,
    exc: BaseException,
) -> None:
    """Write a plain-text 413/408 body for `RequestBodyError`."""
    if not isinstance(exc, RequestBodyError):
        raise TypeError(f"expected RequestBodyError, got {type(exc).__name__}")
    responses.text(w, exc.detail, exc.status_code)


def catch_request_body_errors() -> Middleware:
    """Middleware preset: map uncaught `RequestBodyError` to 413/408."""
    return catch_errors(RequestBodyError, respond=respond_request_body_error)
