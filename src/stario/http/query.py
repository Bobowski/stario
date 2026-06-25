"""Parsed query strings: repeated keys become lists; `ParsedQuery` exposes first-value and multi-value APIs."""

from typing import overload
from urllib.parse import unquote_plus as _unquote_plus


def _parse_query(raw: bytes) -> dict[str, list[str]]:
    """
    Parse query string bytes into a multi-value dict.

    Intentionally inline; equivalent to
    `parse_qsl(qs, keep_blank_values=True, separator="&")`.
    """
    data: dict[str, list[str]] = {}
    if not raw:
        return data

    qs = raw.decode("latin-1")
    needs_unquote = "%" in qs or "+" in qs

    for pair in qs.split("&"):
        if not pair:
            continue

        eq = pair.find("=")
        if eq < 0:
            k = pair
            v = ""
        else:
            k = pair[:eq]
            v = pair[eq + 1 :]

        if needs_unquote:
            if "%" in k or "+" in k:
                k = _unquote_plus(k)
            if v and ("%" in v or "+" in v):
                v = _unquote_plus(v)

        if k in data:
            data[k].append(v)
        else:
            data[k] = [v]

    return data


class ParsedQuery:
    """View over parsed query bytes preserving repeated keys.

    Use `get` for the first value, `getlist` / `as_lists` for every value,
    and `as_dict` for one string per key (Pydantic-friendly).
    """

    __slots__ = ("_data",)

    def __init__(self, raw: bytes) -> None:
        """`raw` is query bytes from the URL (no leading `?`), Latin-1 decoded then split on `&`."""
        self._data = _parse_query(raw)

    @overload
    def get(self, key: str) -> str | None: ...

    @overload
    def get[T](self, key: str, default: T) -> str | T: ...

    def get[T](self, key: str, default: T | None = None) -> str | T | None:
        """First value for `key`, or `default` when the key is absent."""
        vals = self._data.get(key)
        return vals[0] if vals else default

    def getlist(self, key: str) -> list[str]:
        """Every value for `key` (empty list if missing), preserving duplicates from the query string."""
        vals = self._data.get(key)
        return list(vals) if vals else []

    def items(self) -> list[tuple[str, str]]:
        """All key-value pairs, flattened."""
        return [(k, v) for k, vals in self._data.items() for v in vals]

    def as_dict(self, *, last: bool = False) -> dict[str, str]:
        """One string per key, suitable for Pydantic `model_validate` and similar.

        Repeated keys (`?a=1&a=2`) keep the **first** value by default (same as
        `get`). Pass `last=True` to keep the last value. For every value as a
        list, use `as_lists`.
        """
        i = -1 if last else 0
        return {k: vals[i] for k, vals in self._data.items()}

    def as_lists(self) -> dict[str, list[str]]:
        """All keys with every repeated value preserved (copy of each list).

        Use with schemas whose fields are `list[str]` (or similar) for
        `?tag=a&tag=b`-style parameters.
        """
        return {k: list(v) for k, v in self._data.items()}

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __bool__(self) -> bool:
        return bool(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ParsedQuery):
            return self._data == other._data
        return NotImplemented

    def __repr__(self) -> str:
        return f"ParsedQuery({self._data!r})"
