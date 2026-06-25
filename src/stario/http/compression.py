"""HTTP content-coding negotiation and compressor implementations."""

import zlib
from collections.abc import Iterable
from compression import zstd
from typing import Any, ClassVar, cast

import brotli  # pyright: ignore[reportMissingTypeStubs]

from stario._env import env_int, env_optional_int
from stario.exceptions import StarioError

from .headers import Headers
from .wire import parse_accept_encoding

# brotli is an untyped C extension; alias as Any so strict pyright stays quiet
# without vendoring or tracking third-party stub packages.
_brotli: Any = brotli


def brotli_decompress(data: bytes) -> bytes:
    return cast(bytes, _brotli.decompress(data))


# `CompressionConfig.select` honors `Accept-Encoding` q-values, then prefers
# br -> zstd -> gzip when the client weights supported encodings equally.
# zstd uses the 3.14 `compression.zstd` API; brotli/gzip cover older clients.

_ZSTD_WINDOW_LOG_MIN, _ZSTD_WINDOW_LOG_MAX = (
    zstd.CompressionParameter.window_log.bounds()
)
_BROTLI_WINDOW_LOG_MIN = 10
_BROTLI_WINDOW_LOG_MAX = 24
_GZIP_WINDOW_BITS_MIN = 9
_GZIP_WINDOW_BITS_MAX = 15
_ENCODING_PREFERENCE = (b"br", b"zstd", b"gzip")


def negotiate_content_encoding(
    accept_encoding: str | bytes | None,
    available: Iterable[bytes],
) -> bytes | None:
    """Choose the best available content-coding from `Accept-Encoding`."""
    if accept_encoding is None:
        return None

    accepted = parse_accept_encoding(accept_encoding)
    wildcard_q = accepted.get(b"*", 0.0)
    available_set = set(available)
    best_q = 0.0
    best_encoding: bytes | None = None

    for encoding in _ENCODING_PREFERENCE:
        if encoding not in available_set:
            continue
        q = accepted.get(encoding, wildcard_q)
        if q > best_q:
            best_q = q
            best_encoding = encoding

    if best_encoding is None:
        return None

    identity_q = accepted.get(b"identity")
    if identity_q is not None and identity_q >= best_q:
        return None

    return best_encoding


def merge_vary(headers: Headers, token: bytes) -> None:
    """Append `token` to `Vary` without dropping existing field names."""
    existing = headers.unsafe_get(b"vary")
    if existing is None:
        headers.unsafe_set(b"vary", token)
        return

    stripped = existing.strip()
    if not stripped:
        headers.unsafe_set(b"vary", token)
        return
    if stripped == b"*":
        return

    token_lower = token.lower()
    has_value = False
    for raw_part in existing.split(b","):
        part = raw_part.strip()
        if not part:
            continue
        has_value = True
        if part == b"*" or part.lower() == token_lower:
            return

    if has_value:
        headers.unsafe_set(b"vary", existing.rstrip() + b", " + token)
    else:
        headers.unsafe_set(b"vary", token)


_NONCOMPRESSIBLE_CONTENT_TYPE_PREFIXES = (
    b"image/",
    b"audio/",
    b"video/",
)

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


def content_type_is_compressible(content_type: bytes) -> bool:
    """Return False for missing types, already-compressed/binary media types."""
    media_type = content_type.split(b";", 1)[0].strip().lower()
    if not media_type:
        return False

    if media_type in _NONCOMPRESSIBLE_CONTENT_TYPES:
        return False

    return not media_type.startswith(_NONCOMPRESSIBLE_CONTENT_TYPE_PREFIXES)


_ZSTD_FLUSH_BLOCK = 1  # zstd block flush for incremental chunked HTTP


class Compressor:
    """
    Base compressor with shared logic.

    Subclasses implement frame() for one-shot and block() for streaming.
    """

    __slots__ = ("_level", "_stream", "_window")
    encoding: ClassVar[bytes] = b""

    def __init__(
        self,
        level: int,
        *,
        window: int | None = None,
    ) -> None:
        self._level = level
        self._window = window
        self._stream: Any = None

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
        return self._stream.compress(data, mode=_ZSTD_FLUSH_BLOCK)

    def finish(self) -> bytes:
        if self._stream is None:
            return b""
        return self._stream.flush()


class _Brotli(Compressor):
    """Brotli - great ratio, excellent browser support."""

    encoding = b"br"

    def __init__(
        self,
        level: int,
        *,
        window: int | None = None,
    ) -> None:
        super().__init__(level, window=window)
        self._stream = None

    def frame(self, data: bytes) -> bytes:
        if self._window is not None:
            return cast(
                bytes,
                _brotli.compress(data, quality=self._level, lgwin=self._window),
            )
        return cast(bytes, _brotli.compress(data, quality=self._level))

    def block(self, data: bytes) -> bytes:
        if self._stream is None:
            if self._window is not None:
                self._stream = _brotli.Compressor(
                    quality=self._level,
                    lgwin=self._window,
                )
            else:
                self._stream = _brotli.Compressor(quality=self._level)
        stream = self._stream
        return cast(bytes, stream.process(data) + stream.flush())

    def finish(self) -> bytes:
        if self._stream is None:
            return b""
        return cast(bytes, self._stream.finish())


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


DEFAULT_MIN_SIZE = 512
DEFAULT_ZSTD_LEVEL = 3
DEFAULT_ZSTD_WINDOW_LOG: int | None = None
DEFAULT_BROTLI_LEVEL = 4
DEFAULT_BROTLI_WINDOW_LOG: int | None = None
DEFAULT_GZIP_LEVEL = 6
DEFAULT_GZIP_WINDOW_BITS: int | None = None


class CompressionConfig:
    """Policy for picking `br` / `zstd` / `gzip` from `Accept-Encoding` and for streaming vs whole-body responses.

    Configure only via the constructor; fields are not meant for post-init mutation.
    """

    __slots__ = (
        "_enabled_encodings",
        "brotli_level",
        "brotli_window_log",
        "gzip_level",
        "gzip_window_bits",
        "min_size",
        "zstd_level",
        "zstd_window_log",
    )

    def __init__(
        self,
        *,
        min_size: int = DEFAULT_MIN_SIZE,
        zstd_level: int = DEFAULT_ZSTD_LEVEL,
        zstd_window_log: int | None = DEFAULT_ZSTD_WINDOW_LOG,
        brotli_level: int = DEFAULT_BROTLI_LEVEL,
        brotli_window_log: int | None = DEFAULT_BROTLI_WINDOW_LOG,
        gzip_level: int = DEFAULT_GZIP_LEVEL,
        gzip_window_bits: int | None = DEFAULT_GZIP_WINDOW_BITS,
    ) -> None:
        if min_size < 0:
            raise StarioError(
                "min_size must be 0 or greater",
                help_text="Use 0 to compress all bodies or a positive byte threshold.",
            )
        if zstd_level >= 0 and not 1 <= zstd_level <= 22:
            raise StarioError(
                "zstd_level must be negative or between 1 and 22",
                help_text="Use -1 to disable zstd or a level in 1-22.",
            )
        if zstd_window_log is not None and not (
            _ZSTD_WINDOW_LOG_MIN <= zstd_window_log <= _ZSTD_WINDOW_LOG_MAX
        ):
            raise StarioError(
                "zstd_window_log must be between "
                f"{_ZSTD_WINDOW_LOG_MIN} and {_ZSTD_WINDOW_LOG_MAX}",
                help_text="Omit the setting to use the codec default.",
            )
        if brotli_level >= 0 and not 0 <= brotli_level <= 11:
            raise StarioError(
                "brotli_level must be negative or between 0 and 11",
                help_text="Use -1 to disable brotli or a quality in 0-11.",
            )
        if brotli_window_log is not None and not (
            _BROTLI_WINDOW_LOG_MIN <= brotli_window_log <= _BROTLI_WINDOW_LOG_MAX
        ):
            raise StarioError(
                "brotli_window_log must be between "
                f"{_BROTLI_WINDOW_LOG_MIN} and {_BROTLI_WINDOW_LOG_MAX}",
                help_text="Omit the setting to use the codec default.",
            )
        if gzip_level >= 0 and not 1 <= gzip_level <= 9:
            raise StarioError(
                "gzip_level must be negative or between 1 and 9",
                help_text="Use -1 to disable gzip or a level in 1-9.",
            )
        if gzip_window_bits is not None and not (
            _GZIP_WINDOW_BITS_MIN <= gzip_window_bits <= _GZIP_WINDOW_BITS_MAX
        ):
            raise StarioError(
                "gzip_window_bits must be between "
                f"{_GZIP_WINDOW_BITS_MIN} and {_GZIP_WINDOW_BITS_MAX}",
                help_text="Omit the setting to use the framework default.",
            )

        self.min_size = min_size
        self.zstd_level = zstd_level
        self.zstd_window_log = zstd_window_log
        self.brotli_level = brotli_level
        self.brotli_window_log = brotli_window_log
        self.gzip_level = gzip_level
        self.gzip_window_bits = gzip_window_bits
        enabled: list[bytes] = []
        if brotli_level >= 0:
            enabled.append(b"br")
        if zstd_level >= 0:
            enabled.append(b"zstd")
        if gzip_level >= 0:
            enabled.append(b"gzip")
        self._enabled_encodings = tuple(enabled)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CompressionConfig):
            return NotImplemented
        return (
            self.min_size == other.min_size
            and self.zstd_level == other.zstd_level
            and self.zstd_window_log == other.zstd_window_log
            and self.brotli_level == other.brotli_level
            and self.brotli_window_log == other.brotli_window_log
            and self.gzip_level == other.gzip_level
            and self.gzip_window_bits == other.gzip_window_bits
        )

    def enabled_encodings(self) -> tuple[bytes, ...]:
        """Supported encodings in framework preference order."""
        return self._enabled_encodings

    def make_compressor(self, encoding: bytes) -> Compressor:
        """Build a compressor for an encoding already selected by negotiation."""
        if encoding == b"br" and self.brotli_level >= 0:
            return _Brotli(self.brotli_level, window=self.brotli_window_log)
        if encoding == b"zstd" and self.zstd_level >= 0:
            return _Zstd(self.zstd_level, window=self.zstd_window_log)
        if encoding == b"gzip" and self.gzip_level >= 0:
            return _Gzip(self.gzip_level, window=self.gzip_window_bits)
        raise StarioError(
            f"Unsupported or disabled content encoding: {encoding!r}",
            help_text="Pick an encoding enabled by CompressionConfig levels.",
        )

    def may_compress(
        self,
        accept_encoding: str | bytes | None,
        *,
        data: bytes | None = None,
        content_type: bytes | None = None,
        streaming: bool = False,
    ) -> bool:
        """Return True when negotiation could pick a compressor (shared by `select` and Writer fast paths)."""
        if not self.enabled_encodings():
            return False
        if accept_encoding is None:
            return False
        if content_type is not None and not content_type_is_compressible(content_type):
            return False
        return not (not streaming and (data is None or len(data) < self.min_size))

    def select(
        self,
        accept_encoding: str | bytes | None,
        *,
        data: bytes | None = None,
        content_type: bytes | None = None,
        streaming: bool = False,
    ) -> Compressor | None:
        """Choose one compressor from the client header and this config, or return `None` (identity)."""
        if not self.may_compress(
            accept_encoding,
            data=data,
            content_type=content_type,
            streaming=streaming,
        ):
            return None

        best_encoding = negotiate_content_encoding(
            accept_encoding, self.enabled_encodings()
        )
        if best_encoding is None:
            return None

        return self.make_compressor(best_encoding)


def compression_config_from_env() -> CompressionConfig:
    """Read `STARIO_COMPRESS_*` codec levels and thresholds."""
    try:
        return CompressionConfig(
            min_size=env_int("STARIO_COMPRESS_MIN_SIZE", DEFAULT_MIN_SIZE),
            zstd_level=env_int("STARIO_COMPRESS_ZSTD_LEVEL", DEFAULT_ZSTD_LEVEL),
            zstd_window_log=env_optional_int("STARIO_COMPRESS_ZSTD_WINDOW_LOG"),
            brotli_level=env_int("STARIO_COMPRESS_BROTLI_LEVEL", DEFAULT_BROTLI_LEVEL),
            brotli_window_log=env_optional_int("STARIO_COMPRESS_BROTLI_WINDOW_LOG"),
            gzip_level=env_int("STARIO_COMPRESS_GZIP_LEVEL", DEFAULT_GZIP_LEVEL),
            gzip_window_bits=env_optional_int("STARIO_COMPRESS_GZIP_WINDOW_BITS"),
        )
    except ValueError as exc:
        raise StarioError(str(exc)) from exc
