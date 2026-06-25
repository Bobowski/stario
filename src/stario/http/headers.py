"""Small HTTP header container used by the parser, writer, and application API.

Internally, header names and values are stored as `bytes`, matching HTTP's
Latin-1-compatible wire form. Names are always lowercased for case-insensitive
lookup; values keep their original bytes.

Use `set` / `add` / `get` for normal application code. They accept only `str`
names and values, validate against header injection, and encode to wire bytes.
The `unsafe_*` methods are for parser/writer paths that already have lowercased,
safe bytes and want to skip repeated validation.

`_encode_header_name` and `encode_header_value` are wire helpers used by
`Headers` and tests; prefer the public `Headers` methods in application code.
"""

from functools import lru_cache
from typing import cast

_MISSING = object()

# =============================================================================
# VALIDATION
# Names follow RFC 9110 `token`: visible ASCII tchars only.
# Values reject control characters that can split or smuggle headers. HTAB is
# allowed, and obs-text bytes (0x80-0xFF) are preserved as HTTP permits.
# =============================================================================

_VALID_NAME_BYTES = (
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_VALID_VALUE_BYTES = bytes(
    b for b in range(256) if b == 0x09 or (b >= 0x20 and b != 0x7F)
)


@lru_cache(maxsize=1024)
def _encode_header_name(name: str) -> bytes:
    """Validate and return a lowercased wire-name for public header APIs."""
    name_bytes = name.encode("latin-1")
    if not name_bytes:
        raise ValueError("Invalid header name: empty")
    if name_bytes.translate(None, _VALID_NAME_BYTES):
        raise ValueError(f"Invalid header name: {name}")
    return name_bytes.lower()


def encode_header_value(value: str) -> bytes:
    """Validate and return wire bytes for a header value."""
    value_bytes = value.encode("latin-1")
    if value_bytes.translate(None, _VALID_VALUE_BYTES):
        raise ValueError(f"Invalid header value: {value}")
    return value_bytes


# =============================================================================
# HEADERS CLASS
# =============================================================================


class Headers:
    """Case-insensitive HTTP headers with a bytes-backed internal store.

    `_data` maps lowercased header-name bytes to either one value (`bytes`) or
    multiple values (`list[bytes]`). The single-value shape keeps common headers
    cheap; the list shape preserves duplicates such as `Set-Cookie`.

    `_data` is a stable internal layout for protocol/writer hot paths.
    """

    __slots__ = ("_data",)

    def __init__(
        self, raw_header_data: dict[bytes, bytes | list[bytes]] | None = None
    ) -> None:
        """Build an empty map or wrap already-normalized wire-form headers.

        `raw_header_data` is an internal fast path for the parser and tests. It
        is trusted as-is: names must already be lowercased bytes, and values must
        already be safe bytes. Non-lowercase keys are not normalized; lookups
        will miss. The dict is stored by reference; mutations alias the caller's
        mapping. Application code should use `set` / `add`.
        """
        self._data = raw_header_data if raw_header_data is not None else {}

    # -------------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------------

    def add(self, name: str, value: str) -> None:
        """Append a validated value, keeping prior values for the same name."""
        self.unsafe_add(_encode_header_name(name), encode_header_value(value))

    def unsafe_add(self, name: bytes, value: bytes) -> None:
        """Append wire bytes; `name` must already be lowercased."""
        data = self._data
        if name not in data:
            data[name] = value
            return
        existing = data[name]
        if isinstance(existing, list):
            existing.append(value)
        else:
            data[name] = [existing, value]

    def set(self, name: str, value: str) -> None:
        """Replace all values for `name` with one validated value."""
        self.unsafe_set(_encode_header_name(name), encode_header_value(value))

    def unsafe_set(self, name: bytes, value: bytes) -> None:
        """Set wire bytes for `name`; no validation."""
        self._data[name] = value

    def setdefault(self, name: str, value: str) -> str:
        """Return the first value, or set and return `value` when absent."""
        key = _encode_header_name(name)
        existing = self.unsafe_get(key, _MISSING)
        if existing is not _MISSING:
            return cast(bytes, existing).decode("latin-1")
        val = encode_header_value(value)
        self.unsafe_set(key, val)
        return val.decode("latin-1")

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    def get[T](self, name: str, default: T = None) -> str | T:
        """Return the first value as `str`, or `default` when missing."""
        wire = self.unsafe_get(_encode_header_name(name), _MISSING)
        if wire is _MISSING:
            return default
        return cast(bytes, wire).decode("latin-1")

    def unsafe_get[T](self, name: bytes, default: T = None) -> T | bytes:
        """Return the first wire value as `bytes`."""
        value = self._data.get(name)
        if value is None:
            return default
        # `type is bytes` fast path; lists only come from `unsafe_add`.
        if type(value) is bytes:
            return value
        return cast(list[bytes], value)[0]

    def getlist(self, name: str) -> list[str]:
        """Return every value for `name` as strings."""
        return [
            v.decode("latin-1") for v in self.unsafe_getlist(_encode_header_name(name))
        ]

    def unsafe_getlist(self, name: bytes) -> list[bytes]:
        """Return every wire value for `name` as bytes (copy prevents aliasing)."""
        value = self._data.get(name)
        if value is None:
            return []
        if type(value) is bytes:
            return [value]
        return list(cast(list[bytes], value))

    # -------------------------------------------------------------------------
    # Remove
    # -------------------------------------------------------------------------

    def remove(self, name: str) -> None:
        """Remove all values for `name`."""
        self.unsafe_remove(_encode_header_name(name))

    def unsafe_remove(self, name: bytes) -> None:
        """Remove all wire values for `name`."""
        self._data.pop(name, None)

    # -------------------------------------------------------------------------
    # Iterate
    # -------------------------------------------------------------------------

    def items(self) -> list[tuple[str, str]]:
        """Return flattened `(name, value)` pairs as lowercased strings."""
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in self.unsafe_items()
        ]

    def unsafe_items(self) -> list[tuple[bytes, bytes]]:
        """Return flattened `(name, value)` pairs as wire bytes."""
        result: list[tuple[bytes, bytes]] = []
        for name, value in self._data.items():
            if isinstance(value, list):
                for v in value:
                    result.append((name, v))
            else:
                result.append((name, value))
        return result

    def unsafe_iter_wire_pairs(self) -> list[tuple[bytes, bytes | list[bytes]]]:
        """Return `(name, value)` wire pairs without flattening multi-value headers."""
        return list(self._data.items())

    def unsafe_append_wire_lines(self, parts: list[bytes]) -> None:
        """Append header lines to `parts` for protocol/writer hot paths."""
        for name, value in self._data.items():
            if type(value) is bytes:
                parts.append(name)
                parts.append(b": ")
                parts.append(value)
                parts.append(b"\r\n")
                continue
            for header_value in cast(list[bytes], value):
                parts.append(name)
                parts.append(b": ")
                parts.append(header_value)
                parts.append(b"\r\n")

    # -------------------------------------------------------------------------
    # Protocol
    # -------------------------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        """Whether `name` is present (case-insensitive)."""
        try:
            key = _encode_header_name(name)
        except ValueError:
            return False
        return key in self._data

    def __len__(self) -> int:
        """Number of distinct header names (not total field lines)."""
        return len(self._data)

    def __repr__(self) -> str:
        return f"Headers({self._data!r})"
