"""Bytes-on-wire transforms and bounded LRU caches for hot parse paths."""

from functools import lru_cache

_HEX_DIGITS = frozenset("0123456789ABCDEFabcdef")
_PATH_CACHE_MAX_BYTES = 512
_ACCEPT_ENCODING_CACHE_MAX_BYTES = 512


@lru_cache(maxsize=16)
def decode_method(method_bytes: bytes) -> str:
    return method_bytes.decode("ascii")


def _decode_path_segment(segment: str) -> str:
    if "%" not in segment:
        return segment

    out = bytearray()
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if ch != "%":
            out.append(ord(ch))
            i += 1
            continue
        if i + 2 >= n:
            raise ValueError("invalid percent-encoding in request path")
        hex_digits = segment[i + 1 : i + 3]
        if hex_digits[0] not in _HEX_DIGITS or hex_digits[1] not in _HEX_DIGITS:
            raise ValueError("invalid percent-encoding in request path")
        out.append(int(hex_digits, 16))
        i += 3

    try:
        decoded = out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid UTF-8 in request path") from exc
    if "\x00" in decoded:
        raise ValueError("request path contains a NUL byte")
    return decoded


def _decode_path_uncached(path_bytes: bytes) -> str:
    path: str = path_bytes.decode("ascii")
    if "%" in path:
        # Decode each segment independently and keep encoded slashes encoded so
        # percent-encoding cannot change route structure before trie matching.
        parts: list[str] = []
        for segment in path.split("/"):
            decoded = _decode_path_segment(segment)
            parts.append(decoded.replace("/", "%2F"))
        path = "/".join(parts)
    return path


@lru_cache(maxsize=4096)
def _decode_path_cached(path_bytes: bytes) -> str:
    return _decode_path_uncached(path_bytes)


def decode_path(path_bytes: bytes) -> str:
    """Decode a request path from wire bytes to the canonical str used for routing."""
    if len(path_bytes) > _PATH_CACHE_MAX_BYTES:
        return _decode_path_uncached(path_bytes)
    return _decode_path_cached(path_bytes)


def _parse_accept_encoding_uncached(accept_encoding: bytes) -> dict[bytes, float]:
    parsed: dict[bytes, float] = {}

    if b";" not in accept_encoding:
        for raw_part in accept_encoding.split(b","):
            token = raw_part.strip().lower()
            if token:
                parsed[token] = 1.0
        return parsed

    for raw_part in accept_encoding.split(b","):
        token, sep, params = raw_part.partition(b";")
        token = token.strip().lower()
        if not token:
            continue

        q = 1.0
        while sep:
            param, sep, params = params.partition(b";")
            key, eq, value = param.partition(b"=")
            if eq and key.strip().lower() == b"q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
                break

        parsed[token] = max(0.0, min(1.0, q))
    return parsed


@lru_cache(maxsize=64)
def _parse_accept_encoding_cached(accept_encoding: bytes) -> dict[bytes, float]:
    return _parse_accept_encoding_uncached(accept_encoding)


def parse_accept_encoding(accept_encoding: str | bytes) -> dict[bytes, float]:
    """Parse `Accept-Encoding` into lowercased token bytes -> q-value."""
    if isinstance(accept_encoding, str):
        accept_encoding = accept_encoding.encode("latin-1")
    if len(accept_encoding) > _ACCEPT_ENCODING_CACHE_MAX_BYTES:
        return _parse_accept_encoding_uncached(accept_encoding)
    return _parse_accept_encoding_cached(accept_encoding)
