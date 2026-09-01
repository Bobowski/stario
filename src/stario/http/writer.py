"""Writer protocol: status, headers, and body for one response.

The Cython ``RequestExchange`` implements this. TestClient has its own writer.
There is no Python production writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from stario.http.headers import Headers


class Writer(Protocol):
    """HTTP response for one request: status, headers, and body.

    Set headers on ``headers``, then ``respond`` for a whole body or
    ``write_headers`` followed by ``write`` / ``end`` for streaming.

    ``started`` / ``completed`` are this *response*. ``closing`` is the
    send path: the client is gone, or the app is draining, so further
    writes will not reach an interested peer. After ``end()`` the
    response is complete even if keep-alive is still open (``completed``
    is true, ``closing`` is not). ``write()`` is a no-op if the
    transport is already gone.

    Waiting until the client leaves or the process drains is
    ``c.alive()`` — that cancels the handler task, including work that
    is not a write.
    """

    headers: Headers

    @property
    def status_code(self) -> int | None:
        """HTTP status after headers were sent, else ``None``."""
        ...

    @property
    def started(self) -> bool:
        """``True`` once the status line and headers have been sent."""
        ...

    @property
    def completed(self) -> bool:
        """``True`` after ``end`` / ``respond`` / ``abort`` finished the response."""
        ...

    @property
    def closing(self) -> bool:
        """``True`` when further writes will not reach an interested client."""
        ...

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        """Send a full response in one shot."""
        ...

    def abort(self) -> None:
        """Close without framing a failed started response as complete."""
        ...

    def write_headers(self, status_code: int) -> Self:
        """Send the status line and current ``headers`` (at most once)."""
        ...

    def write(self, data: bytes) -> Self:
        """Write one body chunk; sends default ``200`` headers if needed."""
        ...

    def end(self, data: bytes | None = None) -> None:
        """Finish the response. Optional final body bytes."""
        ...


__all__ = ["Writer"]
