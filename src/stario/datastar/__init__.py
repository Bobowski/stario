"""Flat Datastar namespace: attributes, actions, signal parsing, and SSE event writers."""

import json
from typing import Any, TypedDict

from stario.html import HtmlElement, Script
from stario.http.request import Request

from . import sse as sse
from .actions import ContentType as ContentType
from .actions import RequestCancellation as RequestCancellation
from .actions import Retry as Retry
from .actions import clipboard as clipboard
from .actions import delete as delete
from .actions import fit as fit
from .actions import get as get
from .actions import patch as patch
from .actions import peek as peek
from .actions import post as post
from .actions import put as put
from .actions import set_all as set_all
from .actions import toggle_all as toggle_all
from .attributes import JSEvent as JSEvent
from .attributes import animate as animate
from .attributes import attr as attr
from .attributes import attrs as attrs
from .attributes import bind as bind
from .attributes import class_ as class_
from .attributes import classes as classes
from .attributes import computed as computed
from .attributes import computeds as computeds
from .attributes import custom_validity as custom_validity
from .attributes import effect as effect
from .attributes import ignore as ignore
from .attributes import ignore_morph as ignore_morph
from .attributes import indicator as indicator
from .attributes import init as init
from .attributes import json_signals as json_signals
from .attributes import match_media as match_media
from .attributes import on as on
from .attributes import on_intersect as on_intersect
from .attributes import on_interval as on_interval
from .attributes import on_raf as on_raf
from .attributes import on_resize as on_resize
from .attributes import on_signal_patch as on_signal_patch
from .attributes import persist as persist
from .attributes import preserve_attr as preserve_attr
from .attributes import query_string as query_string
from .attributes import ref as ref
from .attributes import replace_url as replace_url
from .attributes import scroll_into_view as scroll_into_view
from .attributes import show as show
from .attributes import signal as signal
from .attributes import signals as signals
from .attributes import style as style
from .attributes import styles as styles
from .attributes import text as text
from .attributes import view_transition as view_transition
from .format import Case as Case
from .format import js as js
from .format import s as s

DATASTAR_CDN_URL = (
    "https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"
)


class FileSignal(TypedDict):
    """One file field nested inside the signals object (not ``multipart/form-data``).

    See the **File Uploads** section under `data-bind` in the Datastar attributes docs:
    https://data-star.dev/reference/attributes#data-bind

    After loading the signals dict (for example with ``read_signals``), you can narrow one
    entry for type checkers; you must still validate input yourself:

    ```python
    payload = await read_signals(req)
    f: FileSignal = payload["avatar"]
    ```

    ```json
    {"avatar": {"name": "photo.png", "contents": "<base64>", "mime": "image/png"}}
    ```

    ``contents`` is base64 text; ``mime`` may be ``None``.
    """

    name: str
    contents: str
    mime: str | None


def ModuleScript(src: str = DATASTAR_CDN_URL) -> HtmlElement:
    """Load the Datastar client (``type="module"``). Override ``src`` only if you self-host.

    ```python
    from stario import datastar as ds
    from stario.html import HtmlDocument, Head, Body, P

    HtmlDocument(Head(ds.ModuleScript()), Body(P("Hello")))
    # <!doctype html><html><head><script type="module" src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"></script></head><body><p>Hello</p></body></html>
    ```
    """
    return Script({"type": "module", "src": src})


async def read_signals(req: Request) -> dict[str, Any]:
    """Parse the JSON signals blob Datastar sends.

    ``GET`` and ``DELETE`` use the ``datastar`` query parameter; all other methods use the
    request body, matching upstream Datastar after
    https://github.com/starfederation/datastar/pull/1146.

    Convenience only: this uses the stdlib ``json`` module on the raw bytes or query string.
    Incoming signals are untrusted; validate types, sizes, and nested shapes
    (Pydantic, msgspec, cattrs, hand-rolled checks, …). We recommend reading
    ``await req.body()`` or ``req.query.get("datastar", "")`` directly when you want a schema
    library to decode and validate from bytes in one step; that is often faster than
    ``json.loads`` into a plain ``dict`` through this helper.

    ```python
    @app.post("/action")
    async def action(c, w):
        sig = await ds.read_signals(c.req)
        n = int(sig.get("n", 0))
        ds.sse.patch_signals(w, {"n": n + 1})
    ```

    Raises:
        TypeError: If the decoded JSON value is not an object.
        json.JSONDecodeError: If the query string or body is not valid JSON.
    """
    # Datastar only serializes signals into the body for methods other than GET and DELETE
    # (see https://github.com/starfederation/datastar/pull/1146).
    if req.method in ("GET", "DELETE"):
        raw = req.query.get("datastar", "")
    else:
        raw = await req.body()

    if raw in ("", b""):
        return {}

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Signals must decode to a JSON object")
    return value
