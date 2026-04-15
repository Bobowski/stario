"""
Three kinds of failures: intentional HTTP responses (``HttpException`` / ``RedirectException``), framework usage errors (``StarioError``), and dropped connections (``ClientDisconnected``).

``HttpException`` and ``RedirectException`` are re-exported from the ``stario`` package root; prefer ``from stario import HttpException`` or ``RedirectException`` in application code.

Separating them keeps default error handling from treating programmer mistakes like normal 404s, and lets stream handlers distinguish clean disconnects.
"""

from typing import Any

from stario.http.writer import Writer


class StarioError(Exception):
    """
    Prefer this over bare ``Exception`` when the fix is in application/framework usage.

    ``context`` / ``help_text`` / ``example`` are folded into ``str(exc)`` so logs
    and trace events stay actionable without a custom formatter.
    """

    __slots__ = ("message", "context", "help_text", "example")

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        help_text: str | None = None,
        example: str | None = None,
    ) -> None:
        self.message = message
        self.context = context or {}
        self.help_text = help_text
        self.example = example
        super().__init__(self._format())

    def _format(self) -> str:
        """Single string for logging and re-raising."""
        parts = [self.message]

        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            parts.append(f"  Context: {ctx}")

        if self.help_text:
            parts.append(f"  Help: {self.help_text}")

        if self.example:
            parts.append(f"  Example:\n{self.example}")

        return "\n".join(parts)


class StarioRuntime(StarioError):
    """
    Raised when the framework is in a runtime state that makes the requested
    operation invalid.

    Typical examples are writing after the response finished or trying to send
    SSE events after a helper already finalized the response.
    """


class HttpException(Exception):
    """
    Intentional HTTP outcome: ``detail`` is the response body for 4xx/5xx, or ``Location`` for 3xx.

    Prefer ``RedirectException`` for redirects so call sites distinguish URL from body text.

    Registered on ``App`` so handlers can ``raise`` instead of branching on
    ``Writer`` after partial output (still only safe before headers are sent).
    """

    __slots__ = ("status_code", "detail")

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)

    def respond(self, w: Writer) -> None:
        import stario.responses as responses

        if 300 <= self.status_code < 400:
            responses.redirect(w, self.detail, self.status_code)
        else:
            responses.text(w, self.detail or "Error", self.status_code)


class RedirectException(HttpException):
    """HTTP redirect (3xx); ``detail`` is the ``Location`` URL or path (not a response body)."""

    def __init__(self, status_code: int, location: str) -> None:
        if not (300 <= status_code < 400):
            raise StarioError(
                f"RedirectException requires a 3xx status_code, got {status_code}",
                help_text="Use HttpException for response bodies (4xx/5xx).",
            )
        super().__init__(status_code, location)


class ClientDisconnected(Exception):
    """
    The protocol sets a per-connection ``disconnect`` future; ``Writer`` aligns with it.

    Long-running handlers (SSE, chunked) should expect the stream to end without a
    thrown error in some paths—check ``w.disconnected`` / ``w.alive()`` too.
    """

    pass
