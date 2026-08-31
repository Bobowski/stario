"""Query string: same shape as `Headers` — two arrays, linear scan.

On first read the raw `?`-suffix is split and decoded into `_keys` / `_values`.
`get` / `getlist` / `items` walk those arrays. No hash table.

`as_dict` / `as_lists` are grouping *views* over the same arrays (Pydantic /
forms). Headers have no equivalent: they are a pair list (`items` / `getlist`),
not form data.
"""

from typing import overload
from urllib.parse import unquote_plus as _unquote_plus


def _decode_component(raw: bytes, start: int, n: int) -> str:
    if n <= 0:
        return ""
    end = start + n
    i = start
    while i < end:
        c = raw[i]
        if c == 37 or c == 43:
            return _unquote_plus(raw[start:end].decode("latin-1"))
        i += 1
    return raw[start:end].decode("latin-1")


def _parse_query_arrays(raw: bytes) -> tuple[list[str], list[str]]:
    """One pass: decoded keys and values, same order as the wire."""
    if not raw:
        return [], []
    keys: list[str] = []
    values: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        start = i
        eq = -1
        while i < n and raw[i] != 38:
            if eq < 0 and raw[i] == 61:
                eq = i
            i += 1
        end = i
        if start < end:
            if eq < 0:
                keys.append(_decode_component(raw, start, end - start))
                values.append("")
            else:
                keys.append(_decode_component(raw, start, eq - start))
                values.append(_decode_component(raw, eq + 1, end - eq - 1))
        if i < n and raw[i] == 38:
            i += 1
    return keys, values


class ParsedQuery:
    """Query pair list. `get` / `getlist` / `items` match `Headers`."""

    __slots__ = ("_raw", "_keys", "_values")

    def __init__(self, raw: bytes) -> None:
        """`raw` is query bytes from the URL (no leading `?`). Split is deferred."""
        self._raw = raw if raw else b""
        self._keys: list[str] | None = None
        self._values: list[str] | None = None

    def _ensure(self) -> None:
        if self._keys is None:
            self._keys, self._values = _parse_query_arrays(self._raw)
            self._raw = b""

    @overload
    def get(self, key: str) -> str | None: ...

    @overload
    def get[T](self, key: str, default: T) -> str | T: ...

    def get[T](self, key: str, default: T | None = None) -> str | T | None:
        """First value for `key`, or `default` when the key is absent."""
        self._ensure()
        for k, v in zip(self._keys, self._values):
            if k == key:
                return v
        return default

    def getlist(self, key: str) -> list[str]:
        """Every value for `key` (empty list if missing), preserving duplicates."""
        self._ensure()
        return [v for k, v in zip(self._keys, self._values) if k == key]

    def items(self) -> list[tuple[str, str]]:
        """All key-value pairs, flattened (copy of the pair list)."""
        self._ensure()
        return list(zip(self._keys, self._values))

    def as_dict(self, *, last: bool = False) -> dict[str, str]:
        """Group the pair list: one string per key (Pydantic `model_validate`).

        Repeated keys (`?a=1&a=2`) keep the **first** value by default (same as
        `get`). Pass `last=True` to keep the last value. For every value as a
        list, use `as_lists`. Headers have no analogue — use `items` / `getlist`.
        """
        self._ensure()
        out: dict[str, str] = {}
        for k, v in zip(self._keys, self._values):
            if last or k not in out:
                out[k] = v
        return out

    def as_lists(self) -> dict[str, list[str]]:
        """Group the pair list: every repeated value preserved.

        Use with schemas whose fields are `list[str]` (or similar) for
        `?tag=a&tag=b`-style parameters.
        """
        self._ensure()
        out: dict[str, list[str]] = {}
        for k, v in zip(self._keys, self._values):
            existing = out.get(k)
            if existing is None:
                out[k] = [v]
            else:
                existing.append(v)
        return out

    def __contains__(self, key: str) -> bool:
        self._ensure()
        return key in self._keys

    def __bool__(self) -> bool:
        self._ensure()
        return bool(self._keys)

    def __len__(self) -> int:
        self._ensure()
        return len(set(self._keys))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ParsedQuery):
            return self.items() == other.items()
        return NotImplemented

    def __repr__(self) -> str:
        return f"ParsedQuery({self.as_lists()!r})"
