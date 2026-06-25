"""
Failure types:

- `StarioError` — invalid framework or API usage: wrong arguments, invalid
  configuration, or calls that are wrong regardless of object state (uncaught in
  `App` → 500). Examples: bad `UrlPath` params, duplicate route registration,
  invalid bootstrap shape, unfilled `@baked` slots, invalid `Content-Length`.

- `StarioRuntime` — valid API call at the wrong lifecycle phase of a
  framework-managed object during request handling or async session work
  (subclass of `StarioError`; uncaught → 500). The *what* may be fine; the
  *when* is wrong — reorder control flow rather than change a parameter.
  Examples: handler returned without a response, `Writer` used after `end()`,
  request body read twice, SSE after a finalized response, `Relay` subscription
  outside `async with`. Use `StarioRuntime` (not stdlib `RuntimeError`) on the
  request path so `App` matches these in `on_error` via MRO and `str(exc)`
  keeps structured context and help text.

  `on_error(StarioError, …)` matches `StarioRuntime` too unless a more specific
  `on_error(StarioRuntime, …)` is registered.
- `HttpException` / `RedirectException` — intentional HTTP outcomes; `App` maps
  them with `responses.text` / `responses.redirect` in default `on_error` handlers.
- `ClientDisconnected` — peer closed during request body read (`App` aborts the
  connection without a response body).

`HttpException` and `RedirectException` are re-exported from the `stario` package
root; prefer `from stario import HttpException, RedirectException` in application code.

Wrong status codes on the HTTP exception constructors raise `StarioError` (a usage
mistake), not an HTTP response. `RedirectException` validates `location` when the
default handler calls `responses.redirect`, not at construction.

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
    you passed — reorder handler or session control flow to fix it. Register
    `app.on_error(StarioRuntime, …)` to handle these separately from static
    configuration mistakes raised as `StarioError`.
    """


class HttpException(Exception):
    """
    Intentional HTTP response with a plain-text body (4xx/5xx only).

    Registered on `App` so handlers can `raise` instead of branching on
    `Writer` after partial output (still only safe before headers are sent).
    Use `RedirectException` for 3xx redirects so URLs are not confused with
    body text.

    The default `App` handler sends `detail` as `text/plain`, or `"Error"` when
    `detail` is empty.
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

    `location` is the `Location` URL or path. URL safety is checked when the
    default `App` handler calls `responses.redirect`, not at construction. A
    non-3xx `status_code` raises `StarioError` at construction time.
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

    `App` registers a default handler that calls `Writer.abort()` (no response
    body). For long-lived responses (SSE, chunked), prefer polling `c.disconnected`
    or using `c.alive()` instead of relying on this exception.
    """

    def __init__(
        self,
        message: str = "Client closed the connection before the request body finished uploading",
    ) -> None:
        super().__init__(message)
