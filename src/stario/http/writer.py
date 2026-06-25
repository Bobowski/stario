"""
One response object for complete bodies and for chunked streams (including SSE): same header rules, same compression hook.

Whether the client sees `Content-Length` or chunk encoding follows `headers` before the first write. Datastar helpers
emit bytes that go through `write` like any other chunk—no parallel streaming API to learn.
"""

import asyncio
import http
from collections.abc import Callable
from functools import lru_cache
from typing import Self

from stario.exceptions import StarioError, StarioRuntime

from .compression import CompressionConfig, Compressor, merge_vary
from .headers import Headers

_DEFAULT_COMPRESSION = CompressionConfig()
_NO_BODY_STATUSES = frozenset({204, 304})
_RESPONSE_STARTED_ERROR = (
    "Response already started (headers sent). "
    "Set headers via w.headers.set() before the first write or one-shot respond()."
)


def _response_may_have_body(status_code: int) -> bool:
    return status_code not in _NO_BODY_STATUSES and not 100 <= status_code < 200


def _append_wire_headers(parts: list[bytes], headers: Headers) -> None:
    """Append header lines to `parts` for the writer hot path."""
    headers.unsafe_append_wire_lines(parts)


@lru_cache(maxsize=128)
def get_status_line(status_code: int) -> bytes:
    """Build HTTP/1.1 status line."""
    try:
        phrase = http.HTTPStatus(status_code).phrase.encode("ascii")
    except ValueError:
        phrase = b""
    return b"HTTP/1.1 %d %s\r\n" % (status_code, phrase)


class Writer:
    """Low-level HTTP response serializer for one request/response on a connection.

    Set headers on `headers`, then call `respond` for a whole body or
    `write_headers` followed by `write` / `end` for streaming. The response
    helpers and Datastar build on these methods.

    Handlers normally receive a writer from the framework; constructing one
    yourself is for advanced or test code.
    """

    __slots__ = (
        "_accept_encoding",
        "_bytes_written",
        "_completed",
        "_compression",
        "_compressor",
        "_declared_length",
        "_get_date_header",
        "_known_length",
        "_on_completed",
        "_status_code",
        "_transport",
        "headers",
    )

    def __init__(
        self,
        transport: asyncio.Transport,
        get_date_header: Callable[[], bytes],
        on_completed: Callable[[], None],
        compression: CompressionConfig = _DEFAULT_COMPRESSION,
        accept_encoding: str | bytes | None = None,
    ) -> None:
        """Bind the writer to transport I/O for one response on a connection.

        - `transport`: Live asyncio transport for this connection.
        - `get_date_header`: Preformatted `Date: ...\\r\\n` bytes for the status block.
        - `on_completed`: Invoked once when the response is fully finished.
        - `compression`: Policy object shared from `Server` for this connection.
        - `accept_encoding`: Client `Accept-Encoding` header value for negotiation, if any.
        """
        self._transport = transport
        self._get_date_header = get_date_header
        self._on_completed = on_completed

        self._status_code: int | None = None
        self._known_length = False  # True if Content-Length set (no chunking)
        self._declared_length: int | None = None
        self._bytes_written = 0
        self._compression: CompressionConfig = compression
        self._accept_encoding = accept_encoding
        self._compressor: Compressor | None = None
        self._completed = False

        # User can set these:
        self.headers = Headers()

    @property
    def status_code(self) -> int | None:
        """HTTP status code after `write_headers`, else `None`."""
        return self._status_code

    @property
    def started(self) -> bool:
        """`True` once the status line and headers have been sent."""
        return self._status_code is not None

    @property
    def completed(self) -> bool:
        """`True` after `end` has finished the body and completion callback ran."""
        return self._completed

    @property
    def closing(self) -> bool:
        """Whether the connection transport is closing or closed."""
        return self._transport.is_closing()

    def _bind_declared_length(self, length: int) -> None:
        self._declared_length = length
        self._bytes_written = 0

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        """Send a full response in one shot (compression, `Content-Length`, body).

        - `body`: Final entity body bytes.
        - `content_type`: Raw `Content-Type` header value (include `charset` when needed).
        - `status`: HTTP status code.

        Skips negotiation if `Content-Encoding` is already set on `headers`.
        Uses the whole-body compression path, not per-chunk.
        """
        if self._transport.is_closing():
            if not self._completed:
                self._completed = True
                self._on_completed()
            return

        if self._status_code is not None:
            raise StarioRuntime(
                _RESPONSE_STARTED_ERROR,
                help_text=(
                    "Send the response once: use respond(), or write_headers() "
                    "then write()/end()."
                ),
            )

        h = self.headers
        if not _response_may_have_body(status):
            body = b""
        # respond() always sends a complete fixed-size body, even when compression
        # changes the bytes.
        self._known_length = True
        self._bind_declared_length(
            0 if not _response_may_have_body(status) else len(body)
        )

        # Minimal fast path: no custom headers and no compression work.
        if not h and (
            not _response_may_have_body(status)
            or not self._compression.may_compress(
                self._accept_encoding,
                data=body,
                content_type=content_type,
            )
        ):
            if not _response_may_have_body(status):
                parts = (
                    get_status_line(status),
                    self._get_date_header(),
                    b"content-length: 0\r\n\r\n",
                )
            else:
                parts = (
                    get_status_line(status),
                    self._get_date_header(),
                    b"content-type: ",
                    content_type,
                    b"\r\ncontent-length: ",
                    b"%d" % len(body),
                    b"\r\n\r\n",
                    body,
                )
            content = b"".join(parts)
        else:
            # Header-aware path: preserve caller headers, and compress before
            # Content-Length is set.
            if not _response_may_have_body(status):
                body = b""
                h.unsafe_set(b"content-length", b"0")
            elif h.unsafe_get(b"content-encoding") is None:
                compressor = self._compression.select(
                    self._accept_encoding,
                    data=body,
                    content_type=content_type,
                )
                if compressor is not None:
                    body = compressor.frame(body)
                    h.unsafe_set(b"content-encoding", compressor.encoding)
                    merge_vary(h, b"accept-encoding")

            h.unsafe_set(b"content-type", content_type)
            h.unsafe_set(b"content-length", b"%d" % len(body))
            self._bind_declared_length(len(body))

            parts = [get_status_line(status), self._get_date_header()]
            _append_wire_headers(parts, h)
            parts.append(b"\r\n")
            parts.append(body)
            content = b"".join(parts)

        self._transport.write(content)
        # _status_code is the "response started" sentinel, so set it only after
        # bytes are handed off.
        self._status_code = status
        self._bytes_written = self._declared_length or 0
        self._on_completed()
        self._completed = True

    def abort(self) -> None:
        """Close the connection without framing a failed started response as complete."""
        if self._completed:
            return
        self._completed = True
        self.headers.unsafe_set(b"connection", b"close")
        self._transport.close()
        self._on_completed()

    def write_headers(self, status_code: int) -> Self:
        """Send the status line and all current `headers` (must be called at most once).

        - `status_code`: HTTP status for this response.

        `self` for chaining.

        - `StarioRuntime`: If headers were already sent.

        If `Content-Length` is set, the body must be sent as raw bytes. Otherwise
        the writer uses chunked encoding and may pick a streaming compressor.
        """
        if self._transport.is_closing():
            return self

        if self._status_code is not None:
            raise StarioRuntime(
                _RESPONSE_STARTED_ERROR,
                help_text=(
                    "Send the response once: use respond(), or write_headers() "
                    "then write()/end()."
                ),
            )

        headers = self.headers

        # 204/304 and 1xx responses must not use chunked framing or a message body.
        if not _response_may_have_body(status_code):
            headers.unsafe_remove(b"transfer-encoding")
            headers.unsafe_set(b"content-length", b"0")
            self._known_length = True
            self._bind_declared_length(0)
        # Caller-controlled: Content-Length means raw body bytes; otherwise HTTP/1.1
        # chunked framing with optional streaming compression.
        elif headers.unsafe_get(b"content-length") is not None:
            headers.unsafe_remove(b"transfer-encoding")
            self._known_length = True
            raw_length = headers.unsafe_get(b"content-length")
            try:
                self._bind_declared_length(int(raw_length))  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise StarioError(
                    "Invalid Content-Length header",
                    context={"content-length": raw_length},
                    help_text="Set Content-Length to a non-negative integer before write_headers().",
                ) from exc

        else:
            headers.unsafe_set(b"transfer-encoding", b"chunked")
            self._known_length = False
            if (
                headers.unsafe_get(b"content-encoding") is None
                and self._compressor is None
            ):
                self._compressor = self._compression.select(
                    self._accept_encoding,
                    content_type=headers.unsafe_get(b"content-type"),
                    streaming=True,
                )
            if self._compressor is not None:
                headers.unsafe_set(b"content-encoding", self._compressor.encoding)
                merge_vary(headers, b"accept-encoding")

        parts = [get_status_line(status_code), self._get_date_header()]
        # Hot path: read headers._data directly and append wire chunks separately.
        # iter_items() / unsafe_items() add generator or list overhead here; name+value
        # concat allocates an intermediate bytes per line. Four appends + join is fastest.
        _append_wire_headers(parts, headers)
        parts.append(b"\r\n")
        self._transport.write(b"".join(parts))
        self._status_code = status_code

        return self

    def write(self, data: bytes) -> Self:
        """Write one body chunk, sending default `200` headers first if needed.

        - `data`: Chunk of the entity body.

        `self` for chaining.

        - `StarioRuntime`: If `end` already completed the response.

        Status defaults to `200` when streaming starts via the first `write()`.
        In chunked mode, chunk framing and optional compression are automatic.
        """
        if self._transport.is_closing():
            return self

        if self._completed:
            raise StarioRuntime(
                "Cannot write after response is completed. "
                "This happens after calling w.end() or a response helper has "
                "already finalized the writer. "
                "Each handler should only send one response.",
                help_text=(
                    "Send one response per handler: stream with write()/end(), "
                    "or finish with a response helper — not both."
                ),
            )

        if not data:
            return self

        if self._status_code is None:
            self.write_headers(200)

        if self._status_code is not None and not _response_may_have_body(
            self._status_code
        ):
            raise StarioRuntime(
                f"Cannot write a body for HTTP {self._status_code} responses.",
                help_text=(
                    "204/304 and 1xx responses must not include a message body."
                ),
            )

        if self._known_length:
            # Caller promised a fixed body size; chunk framing would violate HTTP/1.1.
            self._bytes_written += len(data)
            self._transport.write(data)
        elif self._compressor is not None:
            compressed = self._compressor.block(data)
            self._transport.write(b"%x\r\n%s\r\n" % (len(compressed), compressed))
        else:
            self._transport.write(b"%x\r\n%s\r\n" % (len(data), data))

        return self

    def end(self, data: bytes | None = None) -> None:
        """Finish the response and run the completion callback.

        - `data`: Final body bytes, or `None`. If the response never started, a
          minimal reply is synthesized (`200` with body, or `204` with empty body).

        The protocol relies on this being called exactly once per handler path so
        keep-alive and pipelining stay correct.
        """
        if self._completed:
            return
        if self._transport.is_closing():
            self._completed = True
            self._on_completed()
            return

        if self._status_code is None:
            # Not started - send minimal response
            has_data = data is not None
            cl = b"%d" % (len(data) if has_data else 0)
            self.headers.unsafe_set(b"content-length", cl)
            self.write_headers(200 if has_data else 204)

        if data:
            self.write(data)

        # Content-Length responses must match bytes actually written.
        if self._declared_length is not None and self._bytes_written != self._declared_length:
            raise StarioRuntime(
                "Response body length mismatch: wrote "
                f"{self._bytes_written} bytes, Content-Length is {self._declared_length}",
                help_text=(
                    "When Content-Length is set, write exactly that many bytes "
                    "before w.end()."
                ),
            )

        if not self._known_length:
            if self._compressor is not None:
                final = self._compressor.finish()
                if final:
                    self._transport.write(b"%x\r\n%s\r\n" % (len(final), final))
            # Terminating chunk; empty body ends the chunked sequence per RFC 9112.
            self._transport.write(b"0\r\n\r\n")

        self._on_completed()
        self._completed = True
