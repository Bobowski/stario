"""
One response object for complete bodies and for chunked streams (including SSE): same header rules, same compression hook.

Whether the client sees ``Content-Length`` or chunk encoding follows ``headers`` before the first write. Datastar helpers
emit bytes that go through ``write`` like any other chunk—no parallel streaming API to learn.
"""

import asyncio
import http
import zlib
from collections.abc import AsyncIterable
from compression import zstd
from dataclasses import dataclass
from functools import lru_cache
from types import TracebackType
from typing import (
    Any,
    AsyncIterator,
    Callable,
    ClassVar,
    Self,
    cast,
    overload,
)

import brotli

from .headers import Headers

# ``CompressionConfig.select`` honors ``Accept-Encoding`` q-values, then prefers
# br → zstd → gzip when the client weights supported encodings equally.
# zstd uses the 3.14 ``compression.zstd`` API; brotli/gzip cover older clients.


_ZSTD_WINDOW_LOG_MIN, _ZSTD_WINDOW_LOG_MAX = (
    zstd.CompressionParameter.window_log.bounds()
)
_BROTLI_WINDOW_LOG_MIN = 10
_BROTLI_WINDOW_LOG_MAX = 24
_GZIP_WINDOW_BITS_MIN = 9
_GZIP_WINDOW_BITS_MAX = 15


def _parse_accept_encoding(accept_encoding: str) -> dict[str, float]:
    """Parse ``Accept-Encoding`` into lowercased token → q-value."""
    parsed: dict[str, float] = {}
    for raw_part in accept_encoding.split(","):
        part = raw_part.strip()
        if not part:
            continue

        token, *params = [segment.strip() for segment in part.split(";")]
        if not token:
            continue

        q = 1.0
        for param in params:
            if not param:
                continue
            key, _, value = param.partition("=")
            if key.strip().lower() != "q":
                continue
            try:
                q = float(value)
            except ValueError:
                q = 0.0
            break

        parsed[token.lower()] = max(0.0, min(1.0, q))
    return parsed


def _encoding_qvalue(parsed: dict[str, float], token: str) -> float:
    if token in parsed:
        return parsed[token]
    return parsed.get("*", 0.0)


_NONCOMPRESSIBLE_CONTENT_TYPE_PREFIXES = (
    b"image/",
    b"audio/",
    b"video/",
)


def _merge_vary(headers: Headers, token: bytes) -> None:
    """Append ``token`` to ``Vary`` without dropping existing field names."""
    if headers.rget(b"vary") == b"*":
        return
    existing = headers.rget(b"vary")
    if existing is None:
        headers.rset(b"vary", token)
        return
    parts = [p.strip() for p in existing.decode("latin-1").split(",") if p.strip()]
    new_tok = token.decode("latin-1")
    if new_tok in parts or "*" in parts:
        return
    merged = ", ".join(parts + [new_tok])
    headers.rset(b"vary", merged.encode("latin-1"))


_NONCOMPRESSIBLE_CONTENT_TYPES = (
    b"application/gzip",
    b"application/x-gzip",
    b"application/zip",
    b"application/x-zip-compressed",
    b"application/x-7z-compressed",
    b"application/vnd.rar",
    b"application/x-rar-compressed",
    b"application/x-bzip",
    b"application/x-bzip2",
    b"application/x-xz",
    b"application/zstd",
    b"application/x-zstd",
    b"font/woff",
    b"font/woff2",
)


def _content_type_is_compressible(content_type: bytes | None) -> bool:
    if content_type is None:
        return True

    media_type = content_type.split(b";", 1)[0].strip().lower()
    if not media_type:
        return True

    if media_type in _NONCOMPRESSIBLE_CONTENT_TYPES:
        return False

    return not media_type.startswith(_NONCOMPRESSIBLE_CONTENT_TYPE_PREFIXES)


class Compressor:
    """
    Base compressor with shared logic.

    Subclasses implement frame() for one-shot and block() for streaming.
    """

    __slots__ = ("_level", "_window", "_min_size", "_stream")
    encoding: ClassVar[bytes] = b""

    def __init__(
        self,
        level: int,
        min_size: int,
        *,
        window: int | None = None,
    ) -> None:
        self._level = level
        self._window = window
        self._min_size = min_size
        self._stream: Any = None

    def compressible(
        self,
        data: bytes,
        content_type: bytes | None = None,
        *,
        streaming: bool = False,
    ) -> bool:
        """Return True if size/type make compression worthwhile."""
        if not _content_type_is_compressible(content_type):
            return False
        if streaming:
            return True
        return len(data) >= self._min_size

    def frame(self, data: bytes) -> bytes:
        """Compress entire body at once (one-shot)."""
        raise NotImplementedError

    def block(self, data: bytes) -> bytes:
        """Compress a chunk/block for streaming (e.g., SSE)."""
        raise NotImplementedError

    def finish(self) -> bytes:
        """Finish a streaming encoder and return any buffered trailer bytes."""
        return b""


class _Zstd(Compressor):
    """Zstandard - Python 3.14 stdlib. Fastest with best ratio."""

    encoding = b"zstd"

    def frame(self, data: bytes) -> bytes:
        if self._window is not None:
            return zstd.compress(
                data,
                options={
                    zstd.CompressionParameter.compression_level: self._level,
                    zstd.CompressionParameter.window_log: self._window,
                },
            )
        return zstd.compress(data, level=self._level)

    def block(self, data: bytes) -> bytes:
        if self._stream is None:
            if self._window is not None:
                self._stream = zstd.ZstdCompressor(
                    options={
                        zstd.CompressionParameter.compression_level: self._level,
                        zstd.CompressionParameter.window_log: self._window,
                    },
                )
            else:
                self._stream = zstd.ZstdCompressor(level=self._level)
        return self._stream.compress(data, mode=1)  # Flush block

    def finish(self) -> bytes:
        if self._stream is None:
            return b""
        return self._stream.flush()


class _Brotli(Compressor):
    """Brotli - great ratio, excellent browser support."""

    encoding = b"br"

    def frame(self, data: bytes) -> bytes:
        if self._window is not None:
            return brotli.compress(data, quality=self._level, lgwin=self._window)
        return brotli.compress(data, quality=self._level)

    def block(self, data: bytes) -> bytes:
        if self._stream is None:
            if self._window is not None:
                self._stream = brotli.Compressor(
                    quality=self._level,
                    lgwin=self._window,
                )
            else:
                self._stream = brotli.Compressor(quality=self._level)
        return self._stream.process(data) + self._stream.flush()

    def finish(self) -> bytes:
        if self._stream is None:
            return b""
        return self._stream.finish()


class _Gzip(Compressor):
    """Gzip - universal fallback, always available."""

    encoding = b"gzip"

    def frame(self, data: bytes) -> bytes:
        c = zlib.compressobj(
            self._level,
            zlib.DEFLATED,
            16 + (self._window if self._window is not None else _GZIP_WINDOW_BITS_MAX),
        )
        return c.compress(data) + c.flush()

    def block(self, data: bytes) -> bytes:
        if self._stream is None:
            self._stream = zlib.compressobj(
                self._level,
                zlib.DEFLATED,
                16
                + (self._window if self._window is not None else _GZIP_WINDOW_BITS_MAX),
            )
        return self._stream.compress(data) + self._stream.flush(zlib.Z_SYNC_FLUSH)

    def finish(self) -> bytes:
        if self._stream is None:
            return b""
        return self._stream.flush(zlib.Z_FINISH)


# =============================================================================
# Compression Configuration
# =============================================================================


@dataclass(slots=True, frozen=True)
class CompressionConfig:
    """Policy for picking ``br`` / ``zstd`` / ``gzip`` from ``Accept-Encoding`` and for streaming vs whole-body responses."""

    min_size: int = 512
    """When body size is known, smaller payloads may skip compression to avoid overhead."""
    zstd_level: int = 3
    """Zstd compression level (1-22). Negative disables offering zstd."""
    zstd_window_log: int | None = None
    """Optional zstd window log (codec bounds apply). ``None`` uses the codec default."""
    brotli_level: int = 4
    """Brotli quality (0-11). Negative disables offering brotli."""
    brotli_window_log: int | None = None
    """Optional Brotli lgwin. ``None`` uses the codec default."""
    gzip_level: int = 6
    """Gzip level (1-9). Negative disables offering gzip."""
    gzip_window_bits: int | None = None
    """Optional gzip window bits. ``None`` uses a framework default."""

    def __post_init__(self) -> None:
        if self.min_size < 0:
            raise ValueError("min_size must be 0 or greater.")
        if self.zstd_level >= 0 and not 1 <= self.zstd_level <= 22:
            raise ValueError("zstd_level must be negative or between 1 and 22.")
        if self.zstd_window_log is not None and not (
            _ZSTD_WINDOW_LOG_MIN <= self.zstd_window_log <= _ZSTD_WINDOW_LOG_MAX
        ):
            raise ValueError(
                "zstd_window_log must be between "
                f"{_ZSTD_WINDOW_LOG_MIN} and {_ZSTD_WINDOW_LOG_MAX}."
            )
        if self.brotli_level >= 0 and not 0 <= self.brotli_level <= 11:
            raise ValueError("brotli_level must be negative or between 0 and 11.")
        if self.brotli_window_log is not None and not (
            _BROTLI_WINDOW_LOG_MIN <= self.brotli_window_log <= _BROTLI_WINDOW_LOG_MAX
        ):
            raise ValueError(
                "brotli_window_log must be between "
                f"{_BROTLI_WINDOW_LOG_MIN} and {_BROTLI_WINDOW_LOG_MAX}."
            )
        if self.gzip_level >= 0 and not 1 <= self.gzip_level <= 9:
            raise ValueError("gzip_level must be negative or between 1 and 9.")
        if self.gzip_window_bits is not None and not (
            _GZIP_WINDOW_BITS_MIN <= self.gzip_window_bits <= _GZIP_WINDOW_BITS_MAX
        ):
            raise ValueError(
                "gzip_window_bits must be between "
                f"{_GZIP_WINDOW_BITS_MIN} and {_GZIP_WINDOW_BITS_MAX}."
            )

    def select(
        self,
        accept_encoding: str | None,
        *,
        data: bytes | None = None,
        content_type: bytes | None = None,
        streaming: bool = False,
    ) -> Compressor | None:
        """Choose one compressor from the client header and this config, or return ``None`` (identity).

        Parameters:
            accept_encoding: Raw ``Accept-Encoding`` header value, or ``None`` to disable compression.
            data: Optional full body; with ``content_type`` or ``streaming``, used to skip tiny or incompressible payloads.
            content_type: Raw ``Content-Type`` bytes; binary types may skip compression.
            streaming: When ``True``, ``min_size`` does not block enabling the codec (chunks are streamed).

        Returns:
            A compressor instance, or ``None`` if negotiation says identity is best.

        Notes:
            Among supported codecs with a positive ``q`` value, the highest ``q`` wins; ties prefer brotli, then zstd, then gzip.
        """
        if self.brotli_level < 0 and self.zstd_level < 0 and self.gzip_level < 0:
            return None

        if accept_encoding is None:
            return None

        accepted = _parse_accept_encoding(accept_encoding)
        choices: list[tuple[float, Compressor]] = []

        if self.brotli_level >= 0:
            q = _encoding_qvalue(accepted, "br")
            if q > 0:
                choices.append(
                    (
                        q,
                        _Brotli(
                            self.brotli_level,
                            self.min_size,
                            window=self.brotli_window_log,
                        ),
                    )
                )

        if self.zstd_level >= 0:
            q = _encoding_qvalue(accepted, "zstd")
            if q > 0:
                choices.append(
                    (
                        q,
                        _Zstd(
                            self.zstd_level,
                            self.min_size,
                            window=self.zstd_window_log,
                        ),
                    )
                )

        if self.gzip_level >= 0:
            q = _encoding_qvalue(accepted, "gzip")
            if q > 0:
                choices.append(
                    (
                        q,
                        _Gzip(
                            self.gzip_level,
                            self.min_size,
                            window=self.gzip_window_bits,
                        ),
                    )
                )

        if not choices:
            return None

        best_q, best = max(choices, key=lambda item: item[0])
        if best_q <= 0:
            return None
        if data is not None or content_type is not None or streaming:
            if not best.compressible(
                data or b"",
                content_type,
                streaming=streaming,
            ):
                return None
        return best


_DEFAULT_COMPRESSION = CompressionConfig()


# =============================================================================
# HTTP Status Line Cache
# =============================================================================


@lru_cache(maxsize=128)
def _get_status_line(status_code: int) -> bytes:
    """Build HTTP/1.1 status line."""
    try:
        phrase = http.HTTPStatus(status_code).phrase.encode("ascii")
    except ValueError:
        phrase = b""
    return b"HTTP/1.1 %d %s\r\n" % (status_code, phrase)


# =============================================================================
# Writer
# =============================================================================


class Writer:
    """Low-level HTTP response serializer for one request/response on a connection.

    Set headers on ``headers``, then either call ``respond`` for a whole body or ``write_headers`` followed by
    ``write`` / ``end`` for streaming (including SSE). The ``stario.responses`` helpers and Datastar build on these methods.

    Notes:
        Handlers normally receive a writer from the framework; constructing one yourself is for advanced or test code.
    """

    __slots__ = (
        "_transport_write",
        "_get_date_header",
        "_disconnect",
        "_shutdown",
        "_on_completed",
        "_status_code",
        "_known_length",
        "_completed",
        "_compression",
        "_accept_encoding",
        "_compressor",
        "headers",
    )

    def __init__(
        self,
        transport_write: Callable[[bytes], None],
        get_date_header: Callable[[], bytes],
        on_completed: Callable[[], None],
        disconnect: asyncio.Future,
        shutdown: asyncio.Future,
        compression: CompressionConfig = _DEFAULT_COMPRESSION,
        accept_encoding: str | None = None,
    ) -> None:
        """Bind the writer to transport I/O and shared disconnect/shutdown futures.

        Parameters:
            transport_write: Callback that writes raw bytes to the connection (e.g. ``transport.write``).
            get_date_header: Preformatted ``Date: ...\\r\\n`` bytes for the status block.
            on_completed: Invoked once when the response is fully finished (keep-alive / pipeline bookkeeping).
            disconnect: Per-connection future completed when the client drops.
            shutdown: Process-wide shutdown future; responses may close the connection when it fires.
            compression: Policy object shared from ``Server`` for this connection.
            accept_encoding: Client ``Accept-Encoding`` header value for negotiation, if any.
        """
        self._transport_write = transport_write
        self._get_date_header = get_date_header
        self._disconnect = disconnect
        self._shutdown = shutdown
        self._on_completed = on_completed

        self._status_code: int | None = None
        self._known_length = False  # True if Content-Length set (no chunking)
        self._compression: CompressionConfig = compression
        self._accept_encoding = accept_encoding
        self._compressor: Compressor | None = None
        self._completed = False

        # User can set these:
        self.headers = Headers()

    # =========================================================================
    # Connection state
    # =========================================================================

    @property
    def status_code(self) -> int | None:
        """HTTP status code after ``write_headers``, else ``None``."""
        return self._status_code

    @property
    def started(self) -> bool:
        """``True`` once the status line and headers have been sent."""
        return self._status_code is not None

    @property
    def completed(self) -> bool:
        """``True`` after ``end`` has finished the body and completion callback ran."""
        return self._completed

    @property
    def disconnected(self) -> bool:
        """``True`` when the client closed the connection (same future ``alive()`` watches)."""
        return self._disconnect.done()

    @property
    def shutting_down(self) -> bool:
        """``True`` when the server is draining (shared shutdown future per process)."""
        return self._shutdown.done()

    @overload
    def alive(self, source: None = None) -> "_Alive[None]": ...

    @overload
    def alive[T](self, source: AsyncIterable[T]) -> "_Alive[T]": ...

    def alive[T](
        self, source: AsyncIterable[T] | None = None
    ) -> "_Alive[T] | _Alive[None]":
        """Watch disconnect and shutdown; cancel the current task when either fires.

        Parameters:
            source: Optional async iterable to iterate inside the same context (SSE loops often pass the event stream).

        Returns:
            An async context manager; also iterable so ``async for x in w.alive(gen):`` works.

        Notes:
            Prefer this over polling ``disconnected`` for long-lived streams.
        """
        return _Alive(self, source)

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        """Send a full response in one shot (compression, ``Content-Length``, body).

        Parameters:
            body: Final entity body bytes.
            content_type: Raw ``Content-Type`` header value (include ``charset`` when needed).
            status: HTTP status code.

        Notes:
            Skips negotiation if ``Content-Encoding`` is already set on ``headers``. Uses the whole-body compression path, not per-chunk.
        """
        h = self.headers
        # Whole-response path: pick one codec before framing (chunked path uses block compression in write()).
        if h.rget(b"content-encoding") is None:
            compressor = self._compression.select(
                self._accept_encoding,
                data=body,
                content_type=content_type,
            )
            if compressor is not None:
                body = compressor.frame(body)
                h.rset(b"content-encoding", compressor.encoding)
                _merge_vary(h, b"accept-encoding")

        h.set(b"content-type", content_type)
        h.rset(b"content-length", b"%d" % len(body))
        self.write_headers(status).end(body)

    # =========================================================================
    # Raw methods (no compression, Go-style)
    # =========================================================================

    def write_headers(self, status_code: int) -> Self:
        """Send the status line and all current ``headers`` (must be called at most once).

        Parameters:
            status_code: HTTP status for this response.

        Returns:
            ``self`` for chaining.

        Raises:
            RuntimeError: If headers were already sent.

        Notes:
            If ``Content-Length`` is set, the body must be sent as raw bytes (no chunk framing). Otherwise the writer uses
            chunked encoding and may pick a streaming compressor from ``Accept-Encoding``.
        """
        if self.disconnected:
            return self

        if self._status_code is not None:
            raise RuntimeError(
                "Response already started (headers sent). "
                "Cannot call write_headers() twice. Headers are sent on first write or when calling one-shot methods. "
                "Set headers via w.headers.set() before any write operations."
            )

        headers = self.headers

        # Caller-controlled: Content-Length => identity body bytes after headers; else HTTP/1.1 chunked.
        if headers.rget(b"content-length") is not None:
            # When we know the length, we don't need to use chunked encoding
            headers.remove(b"transfer-encoding")
            self._known_length = True

        else:
            headers.rset(b"transfer-encoding", b"chunked")
            self._known_length = False
            if headers.rget(b"content-encoding") is None and self._compressor is None:
                self._compressor = self._compression.select(
                    self._accept_encoding,
                    content_type=headers.rget(b"content-type"),
                    streaming=True,
                )
            if self._compressor is not None:
                headers.rset(b"content-encoding", self._compressor.encoding)
                _merge_vary(headers, b"accept-encoding")

        parts = [_get_status_line(status_code), self._get_date_header()]
        append = parts.append

        for name, value in headers._data.items():
            if isinstance(value, bytes):
                append(name)
                append(b": ")
                append(value)
                append(b"\r\n")
            else:
                for v in value:
                    append(name)
                    append(b": ")
                    append(v)
                    append(b"\r\n")

        append(b"\r\n")
        self._transport_write(b"".join(parts))
        self._status_code = status_code

        return self

    def write(self, data: bytes) -> Self:
        """Write one body chunk after headers (or send default ``200`` headers first if not started).

        Parameters:
            data: Chunk of the entity body.

        Returns:
            ``self`` for chaining.

        Raises:
            RuntimeError: If ``end`` already completed the response.

        Notes:
            In chunked mode, chunk framing (and optional compression blocks) is applied automatically.
        """
        if self.disconnected:
            return self

        if self._completed:
            raise RuntimeError(
                "Cannot write after response is completed. "
                "This happens after calling w.end() or a response helper has already finalized the writer. "
                "Each handler should only send one response."
            )

        if self._status_code is None:
            self.write_headers(200)

        if self._known_length:
            # Caller promised a fixed body size; chunk framing would violate HTTP/1.1.
            self._transport_write(data)
        elif self._compressor is not None:
            compressed = self._compressor.block(data)
            self._transport_write(b"%x\r\n%s\r\n" % (len(compressed), compressed))
        else:
            self._transport_write(b"%x\r\n%s\r\n" % (len(data), data))

        return self

    def end(self, data: bytes | None = None) -> None:
        """Finish the response: optional last chunk, terminating chunk for HTTP/1.1 chunked mode, then completion callback.

        Parameters:
            data: Final body bytes, or ``None``. If the response never started, a minimal reply is synthesized (``200`` with
                body, or ``204`` with empty body).

        Notes:
            The protocol relies on this being called exactly once per handler path so keep-alive and pipelining stay correct.
        """
        if self._completed or self.disconnected:
            return

        if self._status_code is None:
            # Not started - send minimal response
            has_data = data is not None
            cl = b"%d" % (len(data) if has_data else 0)
            self.headers.rset(b"content-length", cl)
            self.write_headers(200 if has_data else 204)

        if data:
            self.write(data)

        if not self._known_length:
            if self._compressor is not None:
                final = self._compressor.finish()
                if final:
                    self._transport_write(b"%x\r\n%s\r\n" % (len(final), final))
            # Terminating chunk; empty body ends the chunked sequence per RFC 9112.
            self._transport_write(b"0\r\n\r\n")

        self._on_completed()
        self._completed = True


@dataclass(slots=True)
class _Alive[T]:
    """Connection lifecycle helper bound to response writer."""

    w: Writer
    source: AsyncIterable[T] | None = None
    watcher: asyncio.Task[None] | None = None

    async def __aiter__(self) -> AsyncIterator[T]:
        async with self:
            if self.source is None:
                while True:
                    yield cast(T, None)
            else:
                async for item in self.source:
                    yield item

    async def __aenter__(self) -> "_Alive[T]":
        current_task = asyncio.current_task()
        disconnect = self.w._disconnect
        shutdown = self.w._shutdown

        async def watcher() -> None:
            either = asyncio.Future[None]()

            def trigger(_) -> None:
                if not either.done():
                    either.set_result(None)

            disconnect.add_done_callback(trigger)
            shutdown.add_done_callback(trigger)

            try:
                await either
                if current_task:
                    current_task.cancel()
            finally:
                disconnect.remove_done_callback(trigger)
                shutdown.remove_done_callback(trigger)

        self.watcher = asyncio.create_task(watcher())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        if self.watcher:
            self.watcher.cancel()
            try:
                await self.watcher
            except asyncio.CancelledError:
                pass

        return exc_type is asyncio.CancelledError
