"""
Datastar SSE event formatters.

Simple functions that output SSE event bytes.
No objects, no classes - just bytes out.

Usage:
    from stario.datastar.sse import patch, signals, redirect, script, remove

    w.write(patch(Div({"id": "x"}, "hi"), render))
    w.write(signals({"count": 42}))
"""

import json

from stario.datastar.format import SignalData, signal_to_dict
from stario.html import HtmlElement, SafeString, render
from stario.html import Script as ScriptTag


def patch(
    element: HtmlElement,
    *,
    mode: str = "outer",
    selector: str | None = None,
    use_view_transition: bool = False,
) -> bytes:
    """
    Format datastar-patch-elements SSE event.

    Args:
        element: HTML element to render (Tag, SafeString, str, etc.)
        mode: Patch mode (outer, inner, prepend, append, before, after)
        selector: CSS selector (default: element's id)
        use_view_transition: Use View Transitions API

    Returns:
        SSE event bytes ready to write
    """
    lines = ["event: datastar-patch-elements"]

    if mode != "outer":
        lines.append(f"data: mode {mode}")

    if selector:
        lines.append(f"data: selector {selector}")

    if use_view_transition:
        lines.append("data: useViewTransition true")

    # Render element and handle multiline
    html = render(element)
    for line in html.split("\n"):
        lines.append(f"data: elements {line}")

    return ("\n".join(lines) + "\n\n").encode()


def signals(
    data: SignalData,
    *,
    only_if_missing: bool = False,
) -> bytes:
    """
    Format datastar-patch-signals SSE event.

    Accepts dict, dataclass, Pydantic model, or TypedDict.
    Non-dict types are converted via signal_to_dict().

    Args:
        data: Signal values to update (dict, dataclass, Pydantic, TypedDict)
        only_if_missing: Only set if signal doesn't exist

    Returns:
        SSE event bytes ready to write

    Raises:
        StarioError: If data cannot be converted to JSON-serializable dict
    """
    # Convert to dict if needed (dataclass, Pydantic, etc.)
    signal_dict = signal_to_dict(data)

    lines = ["event: datastar-patch-signals"]

    if only_if_missing:
        lines.append("data: onlyIfMissing true")

    # JSON encode signals
    json_str = json.dumps(signal_dict, separators=(",", ":"), ensure_ascii=False)
    for line in json_str.split("\n"):
        lines.append(f"data: signals {line}")

    return ("\n".join(lines) + "\n\n").encode()


def script(
    code: str,
    *,
    auto_remove: bool = True,
) -> bytes:
    """
    Format script execution SSE event.

    Sends a <script> element that executes on the client.

    Args:
        code: JavaScript code to execute
        auto_remove: Remove script tag after execution

    Returns:
        SSE event bytes ready to write
    """
    attrs = {"data-effect": "el.remove();"} if auto_remove else {}
    element = ScriptTag(attrs, SafeString(code))

    return patch(element, mode="append", selector="body")


def redirect(url: str) -> bytes:
    """
    Format redirect SSE event.

    Navigates browser to new URL via script execution.
    Uses setTimeout to let pending DOM updates complete.

    Args:
        url: URL to redirect to

    Returns:
        SSE event bytes ready to write
    """
    # Use JSON encoding to safely escape URL (handles quotes, backslashes, etc.)
    safe_url = json.dumps(url)
    return script(f"setTimeout(() => window.location = {safe_url});")


def remove(selector: str) -> bytes:
    """
    Format element removal SSE event.

    Removes elements matching the selector from DOM.

    Args:
        selector: CSS selector for elements to remove

    Returns:
        SSE event bytes ready to write
    """
    lines = [
        "event: datastar-patch-elements",
        "data: mode remove",
        f"data: selector {selector}",
    ]
    return ("\n".join(lines) + "\n\n").encode()
