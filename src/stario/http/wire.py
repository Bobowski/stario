"""Bytes-on-wire transforms and bounded LRU caches for hot parse paths."""

from functools import lru_cache

_ACCEPT_ENCODING_CACHE_MAX_BYTES = 512


@lru_cache(maxsize=16)
def decode_method(method_bytes: bytes) -> str:
    return method_bytes.decode("ascii")


def decode_path(path_bytes: bytes) -> str:
    """Fully percent-decode a request path (no query) to ``str``.

    Every ``%XX`` is decoded, ``%2F`` included; routing splits the raw path on
    ``/`` before decoding, so this is only the display/``req.path`` form.
    Raises ``ValueError`` for a malformed escape, invalid UTF-8, or a decoded
    control byte.
    """
    # stario_cython.exchange imports stario.http; resolve on first call.
    from stario_cython.exchange import decode_request_path

    return decode_request_path(path_bytes)


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
