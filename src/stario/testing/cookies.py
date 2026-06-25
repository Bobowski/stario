"""Cookie wire helpers for TestClient (request `Cookie` and response `Set-Cookie`)."""

import http.cookies
from collections.abc import Iterable, Mapping

from stario.cookies import morsels_from_lines
from stario.exceptions import StarioError
from stario.http.headers import Headers


def serialize_cookie_header(cookies: Mapping[str, str]) -> str:
    """Serialize name→value pairs into a `Cookie` header value."""
    jar = http.cookies.SimpleCookie()
    for name, value in cookies.items():
        try:
            jar[name] = value
        except http.cookies.CookieError as exc:
            raise StarioError(
                "Invalid cookie name or value",
                context={"name": name},
                help_text="Cookie names and values must follow RFC 6265 rules.",
            ) from exc
    if not jar:
        return ""
    return "; ".join(morsel.OutputString() for morsel in jar.values())


def parse_set_cookie_headers(set_cookie_values: Iterable[str]) -> dict[str, str]:
    """Parse `Set-Cookie` header lines into a name→value mapping (non-empty values only)."""
    cookies: dict[str, str] = {}
    for name, morsel in morsels_from_lines(set_cookie_values):
        if morsel.value != "":
            cookies[name] = morsel.value
    return cookies


def merge_cookie_jar(jar: dict[str, str], headers: Headers) -> None:
    """Apply `Set-Cookie` response headers to a client cookie jar."""
    for name, morsel in morsels_from_lines(headers.getlist("set-cookie")):
        if morsel["max-age"] == "0" or morsel.value == "":
            jar.pop(name, None)
        else:
            jar[name] = morsel.value
