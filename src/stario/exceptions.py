"""
Failure types:

- `StarioError` — invalid framework or API usage: wrong arguments, invalid
  configuration, or calls that are wrong regardless of object state. Uncaught
  on the request path: logged and the writer is aborted. Examples: bad
  `UrlPath` params, duplicate route registration, invalid bootstrap shape,
  unfilled `@baked` slots, invalid `Content-Length`.

- `StarioRuntime` — valid API call at the wrong lifecycle phase of a
  framework-managed object during request handling or async session work
  (subclass of `StarioError`). Uncaught on the request path: logged and
  the writer is aborted. The *what* may be fine; the
  *when* is wrong — reorder control flow rather than change a parameter.
  Examples: handler returned without a response, `Writer` used after `end()`,
  request body read twice, SSE after a finalized response, `Relay` subscription
  outside `async with`. `str(exc)` keeps structured context and help text.

- `HttpException` / `RedirectException` — types handlers may raise or catch.
  The protocol does not map them to HTTP. Write the response in the handler
  (`responses.text` / `responses.redirect`) or the request is a failure.
- `ClientDisconnected` — peer closed during request body read. The protocol
  logs the failure and aborts if the handler did not finish a response.

Uncaught handler exceptions are logged and abort the writer. They are not
turned into status codes.

`HttpException` and `RedirectException` are re-exported from the `stario` package
root; prefer `from stario import HttpException, RedirectException` in application code.

Wrong status codes on the HTTP exception constructors raise `StarioError` (a usage
mistake), not an HTTP response. `RedirectException` validates `location` when
`responses.redirect` runs, not at construction.

On `StarioError`, `message` is the short summary; `str(exc)` adds context, help, and
example lines for logs and telemetry.
"""

from typing import Any


class StarioError(Exception):
    """
    Prefer this over bare `Exception` when the fix is in application/framework usage.

    `context` / `help_text` / `example` are folded into `str(exc)` so logs
    and trace events stay actionable without a custom formatter.
    """

    __slots__ = ("context", "example", "help_text", "message")

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        help_text: str | None = None,
        example: str | None = None,
    ) -> None:
        self.message = message
        self.context = dict(context) if context else {}
        self.help_text = help_text
        self.example = example
        super().__init__(message)

    def __str__(self) -> str:
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
    Raised when a framework object is used in the wrong lifecycle phase.

    Unlike `StarioError`, the failure is about *when* you called, not *what*
    you passed — reorder handler or session control flow to fix it.
    """


class HttpException(Exception):
    """
    Intentional HTTP response with a plain-text body (4xx/5xx only).

    Handlers may raise this from `body()` / size limits, or catch it and write
    a response. The protocol does not map it to HTTP. Use `RedirectException`
    for 3xx so URLs are not confused with body text.
    """

    __slots__ = ("detail", "status_code")

    def __init__(self, status_code: int, detail: str = "") -> None:
        # HttpException is for error bodies the client should read — not 1xx/2xx
        # continuations and not 3xx redirects (use RedirectException).
        if not 400 <= status_code < 600:
            raise StarioError(
                f"HttpException requires a 4xx or 5xx status code, got {status_code}",
                help_text=(
                    "Use RedirectException for redirects, or responses.text/json/html "
                    "for successful (2xx) bodies."
                ),
            )
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class RedirectException(Exception):
    """
    Intentional HTTP redirect (3xx).

    `location` is the `Location` URL or path. URL safety is checked when
    `responses.redirect` runs, not at construction. A non-3xx `status_code`
    raises `StarioError` at construction time.
    """

    __slots__ = ("location", "status_code")

    def __init__(self, status_code: int, location: str) -> None:
        if not (300 <= status_code < 400):
            raise StarioError(
                f"RedirectException requires a 3xx status_code, got {status_code}",
                help_text="Use HttpException for response bodies (4xx/5xx).",
            )
        self.status_code = status_code
        self.location = location
        super().__init__(location)


class ClientDisconnected(Exception):
    """
    The peer closed the connection while the request body was still being read.

    If the handler does not finish a response, the protocol logs the failure
    and aborts. For long-lived responses (SSE, chunked), prefer polling
    `c.disconnected` or using `c.alive()` instead of relying on this exception.
    """

    def __init__(
        self,
        message: str = "Client closed the connection before the request body finished uploading",
    ) -> None:
        super().__init__(message)
