"""
Whole-response helpers built on top of ``Writer``.

Each helper writes headers and finalizes the response in one step, so handlers can
stay focused on payload shape rather than framing details.
"""

import json as json_module
from typing import Any
from urllib.parse import quote

from stario.exceptions import StarioError
from stario.html import render as render_html
from stario.http.writer import Writer

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def _encode_json(value: JsonValue) -> bytes:
    return json_module.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _render_html_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    return render_html(content).encode("utf-8")


def html(w: Writer, content: Any, status: int = 200) -> None:
    """Send a complete HTML response via ``Writer.respond``.

    Parameters:
        w: Active response writer for this request.
        content: ``bytes``, ``str``, or Stario HTML nodes (nodes run through ``stario.html.render``).
        status: HTTP status code.

    Notes:
        Sets ``Content-Type: text/html; charset=utf-8`` and negotiates compression like other helpers.
    """
    w.respond(_render_html_bytes(content), b"text/html; charset=utf-8", status)


def json(w: Writer, value: JsonValue, status: int = 200) -> None:
    """Serialize ``value`` to compact UTF-8 JSON and finish the response.

    Parameters:
        w: Active response writer.
        value: JSON-serializable structure (``dict``, ``list``, scalars).
        status: HTTP status code.

    Notes:
        Not for NDJSON or streaming; use ``Writer.write`` for line-delimited output.
    """
    w.respond(_encode_json(value), b"application/json; charset=utf-8", status)


def text(w: Writer, text: str, status: int = 200) -> None:
    """Send ``text`` as UTF-8 ``text/plain``.

    Parameters:
        w: Active response writer.
        text: Message body as a Unicode string.
        status: HTTP status code.
    """
    w.respond(text.encode("utf-8"), b"text/plain; charset=utf-8", status)


def redirect(w: Writer, url: str, status: int = 307) -> None:
    """Send a redirect with an empty body and a ``Location`` header.

    Parameters:
        w: Active response writer.
        url: Absolute URL or app-root-relative path (must not start with ``//`` when relative).
        status: Redirect status (``302``, ``303``, ``307``, ``308``, etc.).

    Raises:
        StarioError: If ``url`` contains CR/LF or an unsafe relative form.

    Notes:
        Uses ``Content-Length: 0``; call before other body writes.
    """
    if "\r" in url or "\n" in url:
        raise StarioError(
            "Redirect target must not contain control characters",
            context={"url": url},
            help_text="Remove CR/LF characters from redirect and navigation targets.",
        )

    if url.startswith("/") and (url.startswith("//") or "\\" in url):
        raise StarioError(
            "Relative redirect target must be a safe app-relative path",
            context={"url": url},
            help_text=(
                "Use a path starting with '/' and avoid protocol-relative or "
                "backslash-containing URLs."
            ),
        )

    normalized = quote(str(url), safe=":/%#?=@[]!$&'()*+,;")
    try:
        location = normalized.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise StarioError(
            "Redirect target could not be encoded for the Location header",
            context={"url": url},
            help_text="Use ASCII or percent-encoded UTF-8 in paths and query strings.",
        ) from exc

    w.headers.rset(b"location", location)
    w.headers.rset(b"content-length", b"0")
    w.write_headers(status).end()


def empty(w: Writer, status: int = 204) -> None:
    """Finish with no message body (default ``204 No Content``).

    Parameters:
        w: Active response writer.
        status: Status code; ``204`` is typical for success-without-body.
    """
    w.headers.rset(b"content-length", b"0")
    w.write_headers(status).end()
