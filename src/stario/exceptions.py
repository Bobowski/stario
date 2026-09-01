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

- `RequestBodyError` — request body read failed (413 payload too large or 408
  upload stall). Raised by the Cython exchange. Not mapped to
  HTTP by the protocol; use `stario.http.middleware.catch_errors` (or write the
  response in the handler) to turn it into a client-facing status.

- `RedirectException` — optional handler shortcut for 3xx redirects. The
  protocol does not map it; call `responses.redirect` (or catch and write).

- `ClientDisconnected` — peer closed during request body read. The protocol
  logs the failure and aborts if the handler did not finish a response.

Uncaught handler exceptions are logged. If nothing was sent, the framework
writes **500**; bytes already on the wire are not rewritten.

`RedirectException` is re-exported from the `stario` package root. Import
`RequestBodyError` from `stario.exceptions` when catching body read failures.

Wrong status codes on redirect constructors raise `StarioError` (a usage
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


class RequestBodyError(StarioRuntime):
    """
    Request body read failed: payload too large (413) or upload stalled (408).

    Raised while buffering or streaming the request body. Uncaught in a handler
    becomes a framework **500**; map to the intended status with middleware:

    ```python
    from stario.exceptions import RequestBodyError
    from stario.http.middleware import catch_errors, respond_request_body_error

    app.use("/", catch_errors(RequestBodyError, respond=respond_request_body_error))
    ```
    """

    __slots__ = ("detail", "status_code")

    def __init__(self, status_code: int, detail: str = "") -> None:
        if status_code not in (408, 413):
            raise StarioError(
                f"RequestBodyError requires status 408 or 413, got {status_code}",
                help_text="Use 413 for oversize bodies and 408 for upload stalls.",
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
                help_text="Use responses.text/json/html for response bodies (4xx/5xx).",
            )
        self.status_code = status_code
        self.location = location
        super().__init__(location)


class ClientDisconnected(Exception):
    """
    The peer closed the connection while the request body was still being read.

    If the handler does not finish a response, the protocol logs the failure
    and aborts. For long-lived responses (SSE, chunked), prefer `c.alive()`
    (handler lifetime) or `w.closing` (stop sending) instead of relying on
    this exception.
    """

    def __init__(
        self,
        message: str = "Client closed the connection before the request body finished uploading",
    ) -> None:
        super().__init__(message)
