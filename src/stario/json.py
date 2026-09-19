"""Process-wide JSON codec for responses, Datastar, telemetry, and tests.

`dumps` returns text, `dumps_bytes` returns UTF-8 bytes, and `loads` accepts
text, bytes, or a byte array. Replace the standard-library default with
`set_codec`.
"""

import json as json_module
from collections.abc import Callable
from typing import Protocol

type JsonDefault = Callable[[object], object]


class JsonCodec(Protocol):
    """Serialize JSON as text or UTF-8 bytes and parse bytes or text.

    `dumps(value).encode("utf-8")` and `dumps_bytes(value)` must be identical.
    Both forms must contain one compact RFC 8259 JSON value without a byte-order
    mark or formatting line breaks.

    Stario forwards `default`, but each backend decides which native types it
    handles before calling that function. Dump methods must raise `TypeError` or
    `ValueError` when serialization fails. `loads` must raise `ValueError` when
    the input is not valid JSON.
    """

    def dumps(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> str: ...

    def dumps_bytes(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> bytes: ...

    def loads(self, data: str | bytes | bytearray, /) -> object: ...


class StdlibJsonCodec:
    """Compact UTF-8 JSON implemented with the Python standard library."""

    __slots__ = ()

    def dumps(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> str:
        return json_module.dumps(
            value,
            allow_nan=False,
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def dumps_bytes(
        self,
        value: object,
        /,
        *,
        default: JsonDefault | None = None,
    ) -> bytes:
        return self.dumps(value, default=default).encode("utf-8")

    def loads(self, data: str | bytes | bytearray, /) -> object:
        return json_module.loads(data)


_codec: JsonCodec = StdlibJsonCodec()


def set_codec(codec: JsonCodec) -> None:
    """Replace the codec used by later JSON operations.

    The assignment is not synchronized with active work. Values already
    serialized and operations already using the previous codec do not change.
    """
    global _codec
    _codec = codec


def dumps(
    value: object,
    /,
    *,
    default: JsonDefault | None = None,
) -> str:
    """Serialize a value as compact JSON text."""
    return _codec.dumps(value, default=default)


def dumps_bytes(
    value: object,
    /,
    *,
    default: JsonDefault | None = None,
) -> bytes:
    """Serialize a value as compact UTF-8 JSON bytes."""
    return _codec.dumps_bytes(value, default=default)


def loads(data: str | bytes | bytearray, /) -> object:
    """Parse JSON text, bytes, or a byte array into Python values."""
    return _codec.loads(data)


__all__ = [
    "JsonCodec",
    "JsonDefault",
    "StdlibJsonCodec",
    "dumps",
    "dumps_bytes",
    "loads",
    "set_codec",
]
