"""Middleware helpers for handler wrappers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from stario.exceptions import StarioError
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


_FETCH_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_FETCH_SITES_OK = frozenset({"same-origin", "none"})
_FETCH_SITES_OK_SAME_SITE = _FETCH_SITES_OK | {"same-site"}


def _authority(value: str) -> str:
    """`host[:port]`, lowercased, default ports stripped; "" when no host."""
    value = value.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    # Authority is up to the first `/`; drop any userinfo just in case.
    authority = value.split("/", 1)[0].rsplit("@", 1)[-1].lower()
    if authority.endswith(":80") or authority.endswith(":443"):
        authority = authority.rsplit(":", 1)[0]
    return authority


def same_origin(*, allow_same_site: bool = False) -> Middleware:
    """Reject cross-origin state-changing requests (403).

    For methods other than GET/HEAD/OPTIONS/TRACE:

    - `Sec-Fetch-Site` present: only `same-origin` and `none` pass
      (`same-site` too when `allow_same_site=True`); anything else is 403.
    - `Sec-Fetch-Site` absent, `Origin` present: `null` is 403; otherwise the
      Origin authority must equal the `Host` authority (lowercased, default
      ports 80/443 stripped).
    - Neither header: allowed — non-browser clients send neither.

    This complements SameSite cookies, which older browsers may not send and
    which do not cover every embedding case; it is not a substitute for CSRF
    tokens on flows SameSite cannot reach.
    """

    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            if c.req.method.upper() not in _FETCH_SAFE_METHODS:
                fetch_site = c.req.headers.get("sec-fetch-site")
                if fetch_site is not None:
                    allowed = (
                        _FETCH_SITES_OK_SAME_SITE
                        if allow_same_site
                        else _FETCH_SITES_OK
                    )
                    if fetch_site.lower() not in allowed:
                        w.text("Cross-origin request blocked", 403)
                        return
                else:
                    origin = c.req.headers.get("origin")
                    if origin is not None:
                        host = c.req.headers.get("host") or ""
                        if origin.strip() == "null" or _authority(origin) != _authority(
                            host
                        ):
                            w.text("Cross-origin request blocked", 403)
                            return
            await handler(c, w)

        return wrapped

    return middleware


def security_headers(
    *,
    frame: Literal["DENY", "SAMEORIGIN"] | None = "DENY",
    referrer: str | None = "strict-origin-when-cross-origin",
    hsts: int | None = None,
) -> Middleware:
    """Set baseline security response headers before the handler runs.

    Sets `x-content-type-options: nosniff` always; `x-frame-options` to
    `frame` and `referrer-policy` to `referrer` unless `None`;
    `strict-transport-security: max-age=<hsts>; includeSubDomains` when
    `hsts` (seconds) is given — use it only when the whole site is HTTPS.
    Headers are set before `handler` runs, so a handler may override one.
    """
    if frame is not None and frame not in ("DENY", "SAMEORIGIN"):
        raise StarioError(
            "security_headers frame must be 'DENY', 'SAMEORIGIN' or None",
            context={"frame": frame},
        )
    if hsts is not None and hsts < 0:
        raise StarioError(
            "security_headers hsts must be >= 0",
            context={"hsts": hsts},
        )

    def middleware(handler: Handler) -> Handler:
        async def wrapped(c: Context, w: Writer) -> None:
            w.headers.set("x-content-type-options", "nosniff")
            if frame is not None:
                w.headers.set("x-frame-options", frame)
            if referrer is not None:
                w.headers.set("referrer-policy", referrer)
            if hsts is not None:
                w.headers.set(
                    "strict-transport-security",
                    f"max-age={hsts}; includeSubDomains",
                )
            await handler(c, w)

        return wrapped

    return middleware
