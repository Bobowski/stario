"""Cookie parsing and `Set-Cookie` emission.

Read inbound cookies via `req.cookies` (backed by `parse_cookie_headers`).
Write outbound cookies with `set_cookie` / `delete_cookie` on a `Writer`.
"""

import http.cookies
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Literal

from stario.exceptions import StarioError
from stario.http.writer import Writer


def morsels_from_lines(
    header_values: Iterable[str],
) -> Iterator[tuple[str, http.cookies.Morsel[str]]]:
    """Yield `(name, morsel)` from header field-values; skip malformed lines."""
    for header_value in header_values:
        if not header_value:
            continue
        parsed = http.cookies.SimpleCookie()
        try:
            parsed.load(header_value)
        except http.cookies.CookieError:
            continue
        yield from parsed.items()


def parse_cookie_headers(cookie_strings: Iterable[str]) -> dict[str, str]:
    """Parse `Cookie` header value(s) into a name→value mapping.

      Uses `http.cookies.SimpleCookie` (RFC 6265 cookie-value quoting and
      unquoting). Later header lines win; within one line, later pairs override
    earlier. Malformed lines are skipped (no cookies extracted from that line).
    """
    cookies: dict[str, str] = {}
    for name, morsel in morsels_from_lines(cookie_strings):
        cookies[name] = morsel.value
    return cookies


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
    """Append `Set-Cookie` (returns `w` for chaining).

    `expires` may be:

    * A `datetime` — formatted as an HTTP-date (GMT).
    * A `str` — sent as the raw `Expires` attribute (must already be a valid HTTP-date if you rely on browser parsing).
    * An `int` — treated as a **Unix timestamp in seconds** (UTC); converted to an HTTP-date.

    For relative expiry, prefer `max_age` (seconds from now).
    """
    cookie: http.cookies.BaseCookie[str] = http.cookies.SimpleCookie()
    try:
        cookie[name] = value
    except http.cookies.CookieError as exc:
        raise StarioError(
            "Invalid cookie name or value",
            context={"name": name},
            help_text="Cookie names and values must follow RFC 6265 rules.",
        ) from exc

    if max_age is not None:
        cookie[name]["max-age"] = str(max_age)
    if expires is not None:
        if isinstance(expires, datetime):
            cookie[name]["expires"] = format_datetime(expires, usegmt=True)
        elif isinstance(expires, int):
            cookie[name]["expires"] = format_datetime(
                datetime.fromtimestamp(expires, tz=UTC),
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

    w.headers.unsafe_add(
        b"set-cookie",
        cookie.output(header="").strip().encode("latin-1"),
    )
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
    """Expire a cookie by sending a clearing `Set-Cookie` that matches how it was set.

    Browsers match on name, path, domain, and often `Secure` / `SameSite`; pass the same
    values you used with `set_cookie` so deletion succeeds:

    ```python
    cookies.set_cookie(w, "sid", token, secure=True, httponly=True, path="/app")
    cookies.delete_cookie(w, "sid", secure=True, httponly=True, path="/app")
    ```
    """
    return set_cookie(
        w,
        name,
        "",
        max_age=0,
        path=path,
        domain=domain,
        secure=secure,
        httponly=httponly,
        samesite=samesite,
    )
