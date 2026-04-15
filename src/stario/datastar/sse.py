"""Datastar SSE events on a ``Writer`` (first write sets ``text/event-stream`` headers).

Typical flow: long-lived handler + ``async with w.alive()`` + ``patch_*`` calls.
Docstring ``# SSE:`` lines sketch the bytes written (``event:`` / ``data:``), not HTML pages.

``redirect`` is a **client-side** navigation: it streams a small script patch so the browser sets
``window.location`` after the SSE event—unlike ``responses.redirect()``, it is not an HTTP 3xx response.
"""

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

from stario.exceptions import StarioError, StarioRuntime
from stario.html import render as render_html
from stario.http.writer import Writer

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = Mapping[str, JsonValue]


@lru_cache(maxsize=16)
def _data_mode(mode: str) -> bytes:
    return f"data: mode {mode}".encode("ascii")


@lru_cache(maxsize=128)
def _data_selector(selector: str) -> bytes:
    return f"data: selector {selector}".encode("utf-8")


@lru_cache(maxsize=4)
def _data_namespace(namespace: str) -> bytes:
    return f"data: namespace {namespace}".encode("ascii")


def _render_html_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    return render_html(content).encode("utf-8")


def _encode_json_object(payload: JsonObject | bytes) -> bytes:
    if isinstance(payload, bytes):
        if not payload.lstrip().startswith(b"{"):
            raise StarioError(
                "Signal payload must be a JSON object",
                context={"payload": payload[:100]},
                help_text="Pass a dict or pre-serialized JSON object bytes.",
            )
        return payload

    return json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _normalize_redirect_target(url: str) -> str:
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

    return quote(str(url), safe=":/%#?=@[]!$&'()*+,;")


def _safe_sse_navigation_url(url: str) -> str:
    """Reject dangerous URL schemes for client-side navigation (``javascript:``, ``data:``, …)."""
    normalized = _normalize_redirect_target(url)
    stripped = normalized.lstrip()
    lower = stripped.lower()
    for bad in ("javascript:", "data:", "vbscript:"):
        if lower.startswith(bad):
            raise StarioError(
                "SSE redirect URL uses a forbidden scheme",
                context={"url": url},
                help_text="Use an app-relative path or https: URL for sse.redirect().",
            )
    return normalized


def _ensure_sse_headers(w: Writer) -> None:
    if w.completed:
        raise StarioRuntime(
            "Cannot send SSE events after the response is completed",
            help_text=(
                "Start the Datastar stream before calling response helpers like "
                "`responses.html()`, `responses.redirect()`, `responses.empty()`, or `w.end()`."
            ),
            example=(
                "from stario import datastar as ds\n\n"
                "ds.sse.patch_elements(w, view)\n"
                "ds.sse.patch_signals(w, {'count': 1})"
            ),
        )

    if w.started:
        ct = w.headers.get("content-type")
        if ct is None or "text/event-stream" not in ct.lower():
            raise StarioRuntime(
                "Cannot send Datastar SSE events: response already started without "
                "Content-Type: text/event-stream",
                help_text=(
                    "Call Datastar SSE helpers first for this response, or send SSE before "
                    "any other body or non-SSE Content-Type."
                ),
            )
        return

    w.headers.rset(b"content-type", b"text/event-stream")
    w.headers.rset(b"cache-control", b"no-cache")
    w.headers.rset(b"connection", b"keep-alive")


def _patch_elements_event(
    html: bytes,
    *,
    mode: str = "outer",
    selector: str | None = None,
    namespace: str | None = None,
    use_view_transition: bool = False,
) -> bytes:
    lines = [b"event: datastar-patch-elements"]
    append = lines.append

    if mode != "outer":
        append(_data_mode(mode))

    if selector:
        append(_data_selector(selector))

    if namespace:
        append(_data_namespace(namespace))

    if use_view_transition:
        append(b"data: useViewTransition true")

    # Write multiline HTML as repeated SSE data lines
    for line in html.split(b"\n"):
        append(b"data: elements " + line)

    return b"\n".join(lines) + b"\n\n"


def _patch_signals_event(
    json_bytes: bytes,
    *,
    only_if_missing: bool = False,
) -> bytes:
    lines = [b"event: datastar-patch-signals"]
    append = lines.append

    if only_if_missing:
        append(b"data: onlyIfMissing true")

    for line in json_bytes.split(b"\n"):
        append(b"data: signals " + line)

    return b"\n".join(lines) + b"\n\n"


def _script_event(
    code: str,
    *,
    auto_remove: bool = True,
) -> bytes:
    code_bytes = code.encode("utf-8")
    if auto_remove:
        html = b'<script data-effect="el.remove();">' + code_bytes + b"</script>"
    else:
        html = b"<script>" + code_bytes + b"</script>"
    return _patch_elements_event(html, mode="append", selector="body")


def _redirect_event(url: str) -> bytes:
    safe_url = json.dumps(url)
    return _script_event(f"setTimeout(() => window.location = {safe_url});")


def _remove_event(selector: str) -> bytes:
    lines = [
        b"event: datastar-patch-elements",
        b"data: mode remove",
        _data_selector(selector),
    ]
    return b"\n".join(lines) + b"\n\n"


def patch_elements(
    w: Writer,
    content: Any,
    *,
    mode: Literal["outer", "inner", "prepend", "append", "before", "after"] = "outer",
    selector: str | None = None,
    namespace: Literal["svg", "mathml"] | None = None,
    use_view_transition: bool = False,
) -> None:
    """Stream an element patch (replace, append, …). ``content`` may be bytes, str, or HTML nodes.

    ```python
    async def live(c, w):
        async with w.alive():
            ds.sse.patch_elements(w, h.Div(h.P("Hello")))
            # SSE: event: datastar-patch-elements … data: elements <div><p>Hello</p></div>
            await asyncio.sleep(1)
            ds.sse.patch_elements(w, h.Div(h.P("Updated")), selector="#slot", mode="inner")
            # SSE: … data: mode inner … data: selector #slot … data: elements <div><p>Updated</p></div>
    ```
    """
    _ensure_sse_headers(w)
    w.write(
        _patch_elements_event(
            _render_html_bytes(content),
            mode=mode,
            selector=selector,
            namespace=namespace,
            use_view_transition=use_view_transition,
        )
    )


def patch_signals(
    w: Writer,
    payload: JsonObject | bytes,
    *,
    only_if_missing: bool = False,
) -> None:
    """Push signal updates to the client (merge into the page store).

    ```python
    ds.sse.patch_signals(w, {"count": 2, "status": "ok"})
    # SSE: event: datastar-patch-signals … data: signals {"count":2,"status":"ok"}
    ```
    """
    _ensure_sse_headers(w)
    w.write(
        _patch_signals_event(
            _encode_json_object(payload),
            only_if_missing=only_if_missing,
        )
    )


def redirect(w: Writer, url: str) -> None:
    """Client-side navigation (not an HTTP 3xx): streams a script that sets ``window.location``.

    The browser navigates after it applies the SSE patch—unlike ``responses.redirect()``,
    there is no ``Location`` response header.

    ```python
    ds.sse.redirect(w, "/done")
    # SSE: … data: elements <script data-effect="el.remove();">setTimeout(() => window.location = "/done");</script>
    ```
    """
    _ensure_sse_headers(w)
    w.write(_redirect_event(_safe_sse_navigation_url(url)))


def execute(w: Writer, code: str, *, auto_remove: bool = True) -> None:
    """Run JS on the client by patching a script element (removed by default).

    Convenience over ``patch_elements``: serializes ``code`` into ``<script>`` HTML and
    streams an **append-to-body** patch (same wire path as other element patches).

    ```python
    ds.sse.execute(w, "console.info('tick')")
    # SSE: … data: elements <script data-effect="el.remove();">console.info('tick')</script>
    ```
    """
    _ensure_sse_headers(w)
    w.write(_script_event(code, auto_remove=auto_remove))


def remove(w: Writer, selector: str) -> None:
    """Remove nodes matching ``selector`` via a patch.

    ```python
    ds.sse.remove(w, "#toast")
    # SSE: event: datastar-patch-elements … data: mode remove … data: selector #toast
    ```
    """
    _ensure_sse_headers(w)
    w.write(_remove_event(selector))
