"""
Stario Exceptions.

Exception types:
- StarioError: Framework errors with rich context for debugging
- HttpException: Expected errors that become HTTP responses
- ClientDisconnected: Client closed connection (for streaming handlers)
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stario.http.writer import Writer


class StarioError(Exception):
    """
    Framework error with context for debugging.

    Provides structured error info for both humans and AI agents:
    - message: What went wrong
    - context: Key-value pairs with relevant data
    - help_text: How to fix it
    - example: Working code example

    Example:
        raise StarioError(
            "Invalid attribute type",
            context={"attr": "class", "got": type(val).__name__},
            help_text="Attributes must be str, int, or bool.",
        )
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
        """Format error message with context."""
        parts = [self.message]

        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            parts.append(f"  Context: {ctx}")

        if self.help_text:
            parts.append(f"  Help: {self.help_text}")

        if self.example:
            parts.append(f"  Example:\n{self.example}")

        return "\n".join(parts)


class HttpException(Exception):
    """
    HTTP exception - becomes an HTTP response.

    For errors:
        raise HttpException(404, "Not found")
        raise HttpException(401, "Please log in")

    For redirects:
        raise HttpException(302, "/login")
        raise HttpException(307, "/new-location")
    """

    __slots__ = ("status_code", "detail")

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)

    def respond(self, w: "Writer") -> None:
        if 300 <= self.status_code < 400:
            w.redirect(self.detail, self.status_code)
        else:
            w.text(self.detail or "Error", self.status_code)


class ClientDisconnected(Exception):
    """
    Client disconnected during request handling.

    Raised by Writer.write() when client is gone.
    Streaming handlers can catch this for cleanup.

    Usage:
        async def sse_handler(c: Context, w: Writer) -> None:
            try:
                while True:
                    w.write(b"data: ping\\n\\n")
                    await asyncio.sleep(1)
            except ClientDisconnected:
                pass  # Client closed browser - cleanup if needed
    """

    pass
