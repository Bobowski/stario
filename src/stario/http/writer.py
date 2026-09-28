"""Writer protocol: status, headers, and body for one response.

The Cython ``RequestHandle`` implements this (one per handler call; it raises
once the handler has returned). TestClient has its own writer.
There is no Python production writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from stario.http.headers import Headers


class Writer(Protocol):
    """Low-level HTTP response for one request.

    Set headers on ``headers``, then ``respond`` for a whole body or
    ``write_headers`` followed by ``write`` / ``end`` for streaming.
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
        """Whether the connection is closing or closed."""
        ...

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        """Send a full response in one shot."""
        ...

    def abort(self) -> None:
        """Close without framing a failed started response as complete."""
        ...

    def write_headers(self, status_code: int, *, body: bool = True) -> Self:
        """Send the status line and current ``headers`` (at most once).

        ``body=False`` is for HEAD (and equivalent): keep ``Content-Length`` as
        the entity size without requiring those bytes before ``end()``.
        """
        ...

    def write(self, data: bytes) -> Self:
        """Write one body chunk; sends default ``200`` headers if needed."""
        ...

    async def drain(self) -> None:
        """Wait until the client has taken enough of what was written.

        ``write()`` never blocks, so a loop writing large chunks to a slow
        client would buffer everything in memory. ``await w.drain()`` after
        each chunk keeps that bounded. Returns immediately while the
        connection keeps up, or once it is closed (check ``closing``).
        """
        ...

    async def sendfile(self, fd: int, offset: int, count: int) -> None:
        """Send ``count`` bytes from an open regular file starting at ``offset``.

        Does not call ``end()``. On HTTP/1 cleartext with ``Content-Length``
        (or HTTP/1.0 close-delimited), this uses the event loop's ``sendfile``
        so the kernel can copy file to socket without a userspace buffer.
        HTTP/2, TLS, and compressed/chunked streams cannot take raw file
        bytes; those fall back to ``pread`` + ``write`` + ``drain``.
        """
        ...

    def end(self, data: bytes | None = None) -> None:
        """Finish the response. Optional final body bytes."""
        ...


__all__ = ["Writer"]
