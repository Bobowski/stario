"""
Whole-response helpers built on top of `Writer`.

Each helper writes headers and finalizes the response in one step, so handlers can
stay focused on payload shape rather than framing details.
"""

from json import dumps as json_dumps

from stario.exceptions import StarioError
from stario.http.redirect import normalized_location
from stario.http.writer import Writer
from stario.markup import render
from stario.markup.types import HtmlElement

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

__all__ = [
    "empty",
    "html",
    "json",
    "normalized_location",
    "redirect",
    "text",
]


def html(w: Writer, content: bytes | str | HtmlElement, status: int = 200) -> None:
    """Send a complete HTML response via `Writer.respond`.

    - `w`: Active response writer for this request.
    - `content`: `bytes`, `str`, or Stario markup nodes (nodes run through `stario.markup.render`).
    - `status`: HTTP status code.

    Sets `Content-Type: text/html; charset=utf-8` and negotiates compression like other helpers.
    """
    if isinstance(content, bytes):
        body = content
    elif isinstance(content, str):
        body = content.encode("utf-8")
    else:
        body = render(content).encode("utf-8")
    w.respond(body, b"text/html; charset=utf-8", status)


def json(w: Writer, value: JsonValue, status: int = 200) -> None:
    """Serialize `value` to compact UTF-8 JSON and finish the response.

    - `w`: Active response writer.
    - `value`: JSON-serializable structure (`dict`, `list`, scalars).
    - `status`: HTTP status code.

    Not for NDJSON or streaming; use `Writer.write` for line-delimited output.
    """
    try:
        payload = json_dumps(value, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except TypeError as exc:
        raise StarioError(
            "JSON response value is not serializable",
            context={"value": value},
            help_text="Pass dict, list, str, int, float, bool, or None values only.",
        ) from exc
    w.respond(payload, b"application/json; charset=utf-8", status)


def text(w: Writer, body: str, status: int = 200) -> None:
    """Send `body` as UTF-8 `text/plain`.

    - `w`: Active response writer.
    - `body`: Message body as a Unicode string.
    - `status`: HTTP status code.
    """
    w.respond(body.encode("utf-8"), b"text/plain; charset=utf-8", status)


def redirect(w: Writer, url: str, status: int = 307) -> None:
    """Send a redirect with an empty body and a `Location` header.

    - `w`: Active response writer.
    - `url`: App-root-relative path or absolute `http` / `https` URL.
    - `status`: Redirect status (`302`, `303`, `307`, `308`, etc.).

    Raises `StarioError` if `status` is not 3xx, or if `url` is unsafe.
    Uses `Content-Length: 0`; call before other body writes.
    """
    if not 300 <= status < 400:
        raise StarioError(
            "Redirect status must be a 3xx status code",
            context={"status": status},
            help_text="Use responses.text/json/html for response bodies, or pass a 3xx redirect status.",
        )

    w.headers.set("location", normalized_location(url))
    w.respond(b"", b"text/plain; charset=utf-8", status)


def empty(w: Writer, status: int = 204) -> None:
    """Finish with no message body (default `204 No Content`).

    - `w`: Active response writer.
    - `status`: Status code; `204` is typical for success-without-body.
    """
    w.respond(b"", b"text/plain; charset=utf-8", status)
