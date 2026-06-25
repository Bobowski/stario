"""Read Datastar request signals."""

import json
from typing import Any, TypedDict, cast

from stario.exceptions import StarioError
from stario.http.request import Request


class FileSignal(TypedDict):
    """One file value nested inside a Datastar signals object.

    Datastar encodes file inputs bound with `data-bind` as signal values, not as
    `multipart/form-data`. `contents` is base64 text; `mime` may be `None`.

    ```python
    payload = await read_signals(req)
    avatar: FileSignal = payload["avatar"]
    ```
    """

    name: str
    contents: str
    mime: str | None


async def read_signals(req: Request) -> dict[str, Any]:
    """Parse the JSON signals blob Datastar sends.

    `GET` and `DELETE` use the `datastar` query parameter. Other methods use the
    request body.

    This is convenience parsing only. Incoming signals are untrusted; validate
    types, sizes, and nested shapes before using them as application data. SSE
    signal patches use snake_case top-level keys; nested client state should be
    sent as nested JSON objects.

    ```python
    from stario.datastar import SSE, read_signals

    @app.post("/action")
    async def action(c, w):
        sig = await read_signals(c.req)
        SSE(w).patch_signals({"n": int(sig.get("n", 0)) + 1})
    ```
    """
    if req.method.upper() in ("GET", "DELETE"):
        raw = req.query.get("datastar", "")
    else:
        raw = await req.body()

    if raw in ("", b""):
        return {}

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StarioError(
            "Signals must be valid JSON",
            context={"json_error": exc.msg, "json_position": str(exc.pos)},
            help_text="Datastar signals must be encoded as one JSON object.",
        ) from exc
    if not isinstance(value, dict):
        raise StarioError(
            "Signals must decode to a JSON object",
            context={"decoded_type": type(value).__name__},
            help_text="Datastar signals must be encoded as one JSON object.",
        )
    return cast(dict[str, Any], value)
