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

    This is the outbound message, not the connection. Client disconnect and
    process drain live on ``Context`` (``c.alive``, ``c.disconnected``,
    ``c.closing``). ``started`` / ``completed`` say whether *this response*
    has been sent, not whether the socket is still open. After ``end()`` the
    response is done even if keep-alive is still open; ``write()`` is a
    no-op if the transport is already gone.
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
