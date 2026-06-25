"""Datastar Server-Sent Events bound to one response writer.

Create one `SSE` per response: `sse = SSE(w)`. The stream opens when you call
`sse.open()` or when the first event is written.
"""

import json
from collections.abc import Mapping
from typing import Any, Literal

from stario.exceptions import StarioError, StarioRuntime
from stario.http.redirect import normalized_location
from stario.http.writer import Writer
from stario.markup.render import render
from stario.responses import JsonValue

from .format import require_mapping, validate_signal_name

type JsonObject = Mapping[str, JsonValue]
type PatchMode = Literal[
    "outer", "inner", "replace", "prepend", "append", "before", "after"
]
type Namespace = Literal["svg", "mathml"]

_PATCH_MODE_NAMES = (
    "outer",
    "inner",
    "replace",
    "prepend",
    "append",
    "before",
    "after",
)
_PATCH_MODES = frozenset(_PATCH_MODE_NAMES)
_PATCH_MODE_HELP = "Use one of: " + ", ".join(_PATCH_MODE_NAMES) + "."
_NAMESPACES = ("svg", "mathml")
_EVENT_PATCH_ELEMENTS = b"event: datastar-patch-elements"
_EVENT_PATCH_SIGNALS = b"event: datastar-patch-signals"


def _sse_field_value(name: str, value: str) -> bytes:
    """Encode one logical SSE field value; payload newlines are handled elsewhere."""
    if "\r" in value or "\n" in value:
        raise StarioError(
            "Datastar SSE field values must not contain line breaks",
            context={"field": name, "value": value[:100]},
            help_text=(
                "Only event payloads may be multiline. Keep selector and option "
                "values on one line."
            ),
        )
    return value.encode()


class SSE:
    """Datastar SSE response bound to one `Writer`.

    `open()` sends headers immediately. If you skip it, the first event opens the
    stream. Event bytes still go through `Writer.write()`, so normal streaming
    compression policy applies. Top-level `patch_signals()` keys are snake_case;
    use nested JSON objects for nested state rather than dotted signal paths.
    """

    __slots__ = ("w",)

    def __init__(self, w: Writer) -> None:
        self.w = w

    def open(self) -> None:
        """Send `text/event-stream` headers now, before the first event."""
        self._prepare_headers()
        if not self.w.started:
            self.w.write_headers(200)

    def patch_elements(
        self,
        content: Any,
        *,
        mode: PatchMode | None = None,
        selector: str | None = None,
        namespace: Namespace | None = None,
        view_transition: bool = False,
        view_transition_selector: str | None = None,
    ) -> None:
        """Patch DOM elements with bytes, text, or Stario markup."""
        if mode is not None and mode not in _PATCH_MODES:
            raise StarioError(
                "Unknown Datastar patch mode",
                context={"mode": str(mode)},
                help_text=_PATCH_MODE_HELP,
            )
        if namespace is not None and namespace not in _NAMESPACES:
            raise StarioError(
                "Unknown Datastar patch namespace",
                context={"namespace": str(namespace)},
                help_text="Use 'svg', 'mathml', or omit namespace.",
            )

        lines = [_EVENT_PATCH_ELEMENTS]
        if mode is not None:
            lines.append(f"data: mode {mode}".encode("ascii"))
        if selector:
            lines.append(b"data: selector " + _sse_field_value("selector", selector))
        if namespace:
            lines.append(f"data: namespace {namespace}".encode("ascii"))
        if view_transition:
            lines.append(b"data: useViewTransition true")
        if view_transition_selector is not None:
            lines.append(
                b"data: viewTransitionSelector "
                + _sse_field_value("viewTransitionSelector", view_transition_selector)
            )

        if isinstance(content, bytes):
            html_bytes = content
        elif isinstance(content, str):
            html_bytes = content.encode("utf-8")
        else:
            html_bytes = render(content).encode("utf-8")

        # SSE represents multiline payloads as repeated `data:` lines.
        for line in html_bytes.split(b"\n"):
            lines.append(b"data: elements " + line)

        self._prepare_headers()
        self.w.write(b"\n".join(lines) + b"\n\n")

    def patch_signals(
        self,
        payload: JsonObject,
        *,
        only_if_missing: bool = False,
    ) -> None:
        """Patch Datastar signals from a JSON-shaped mapping."""
        data = dict(require_mapping("patch_signals", payload))
        for key in data:
            validate_signal_name(key)
        json_bytes = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        lines = [_EVENT_PATCH_SIGNALS]
        if only_if_missing:
            lines.append(b"data: onlyIfMissing true")
        for line in json_bytes.split(b"\n"):
            lines.append(b"data: signals " + line)
        self._prepare_headers()
        self.w.write(b"\n".join(lines) + b"\n\n")

    def navigate(self, url: str) -> None:
        """Navigate the browser from an SSE response; not an HTTP redirect."""
        safe_url = json.dumps(normalized_location(url))
        self.execute_script(f"setTimeout(() => window.location = {safe_url});")

    def execute_script(self, code: str, *, auto_remove: bool = True) -> None:
        """Run trusted developer-authored JavaScript by appending a script element.

        The code is streamed verbatim; do not interpolate untrusted input.
        """
        code_bytes = code.encode("utf-8")
        if auto_remove:
            script = b'<script data-effect="el.remove();">' + code_bytes + b"</script>"
        else:
            script = b"<script>" + code_bytes + b"</script>"
        self.patch_elements(script, mode="append", selector="body")

    def remove(self, selector: str) -> None:
        """Remove nodes matching `selector`."""
        lines = [
            _EVENT_PATCH_ELEMENTS,
            b"data: mode remove",
            b"data: selector " + _sse_field_value("selector", selector),
        ]
        self._prepare_headers()
        self.w.write(b"\n".join(lines) + b"\n\n")

    def _prepare_headers(self) -> None:
        w = self.w
        if w.completed:
            raise StarioRuntime(
                "Cannot send SSE events after the response is completed",
                help_text=(
                    "Create and use `SSE(w)` before response helpers like "
                    "`responses.html()`, `responses.redirect()`, `responses.empty()`, or `w.end()`."
                ),
                example=(
                    "from stario.datastar import SSE\n\n"
                    "sse = SSE(w)\n"
                    "sse.patch_elements(view)\n"
                    "sse.patch_signals({'count': 1})"
                ),
            )

        if w.started:
            content_type = w.headers.unsafe_get(b"content-type")
            if content_type is None or b"text/event-stream" not in content_type.lower():
                raise StarioRuntime(
                    "Cannot send Datastar SSE events: response already started without "
                    "Content-Type: text/event-stream",
                    help_text=(
                        "Create `SSE(w)` and send SSE before any other body or "
                        "non-SSE Content-Type."
                    ),
                )
            return

        w.headers.unsafe_set(b"content-type", b"text/event-stream")
        w.headers.unsafe_set(b"cache-control", b"no-cache")
