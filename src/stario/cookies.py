"""Set-Cookie emission and reading cookies from ``Request`` (``req.cookies``)."""

import http.cookies
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Literal

from stario.http.request import Request
from stario.http.writer import Writer


def set_cookie(
    w: Writer,
    name: str,
    value: str,
    *,
    max_age: int | None = None,
    expires: datetime | str | int | None = None,
    path: str = "/",
    domain: str | None = None,
    secure: bool = False,
    httponly: bool = False,
    samesite: Literal["lax", "strict", "none"] | None = "lax",
) -> Writer:
    """Append ``Set-Cookie`` (returns ``w`` for chaining).

    ``expires`` may be:

    * A ``datetime`` — formatted as an HTTP-date (GMT).
    * A ``str`` — sent as the raw ``Expires`` attribute (must already be a valid HTTP-date if you rely on browser parsing).
    * An ``int`` — treated as a **Unix timestamp in seconds** (UTC); converted to an HTTP-date.

    For relative expiry, prefer ``max_age`` (seconds from now).
    """
    cookie: http.cookies.BaseCookie[str] = http.cookies.SimpleCookie()
    cookie[name] = value

    if max_age is not None:
        cookie[name]["max-age"] = str(max_age)
    if expires is not None:
        if isinstance(expires, datetime):
            cookie[name]["expires"] = format_datetime(expires, usegmt=True)
        elif isinstance(expires, int):
            cookie[name]["expires"] = format_datetime(
                datetime.fromtimestamp(expires, tz=timezone.utc),
                usegmt=True,
            )
        else:
            cookie[name]["expires"] = expires
    if path:
        cookie[name]["path"] = path
    if domain:
        cookie[name]["domain"] = domain
    if httponly:
        cookie[name]["httponly"] = True
    if samesite:
        cookie[name]["samesite"] = samesite
    # Browsers require Secure when SameSite=None; apply to the cookie, not only a local flag.
    if secure or samesite == "none":
        cookie[name]["secure"] = True

    w.headers.radd(b"set-cookie", cookie.output(header="").strip().encode("latin-1"))
    return w


def delete_cookie(
    w: Writer,
    name: str,
    *,
    path: str = "/",
    domain: str | None = None,
    secure: bool = False,
    httponly: bool = False,
    samesite: Literal["lax", "strict", "none"] | None = "lax",
) -> Writer:
    """Expire a cookie by sending a clearing ``Set-Cookie`` that matches how it was set.

    Browsers match on name, path, domain, and often ``Secure`` / ``SameSite``; pass the same
    values you used with ``set_cookie`` so deletion succeeds.
    """
    return set_cookie(
        w,
        name,
        "",
        max_age=0,
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        path=path,
        domain=domain,
        secure=secure,
        httponly=httponly,
        samesite=samesite,
    )


def get_cookie(req: Request, name: str) -> str | None:
    """Return one cookie value from the request, or ``None`` when absent."""
    return req.cookies.get(name)


def get_cookies(req: Request) -> dict[str, str]:
    """Return a shallow copy of all parsed request cookies."""
    return dict(req.cookies)
