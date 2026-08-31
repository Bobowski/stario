"""Typing for the Cython query pair list.

Runtime ``ParsedQuery`` is ``stario_cython.exchange.ParsedQuery``. First read
copies the query and indexes C name/value spans; ``get`` / ``getlist`` search
those and decode only the values you ask for. No Python implementation — this
module is for IDEs and typecheckers.
"""

from typing import TYPE_CHECKING, Protocol, overload

if TYPE_CHECKING:

    class ParsedQuery(Protocol):
        @overload
        def get(self, key: str) -> str | None: ...

        @overload
        def get[T](self, key: str, default: T) -> str | T: ...

        def getlist(self, key: str) -> list[str]: ...
        def items(self) -> list[tuple[str, str]]: ...
        def as_dict(self, *, last: bool = False) -> dict[str, str]: ...
        def as_lists(self) -> dict[str, list[str]]: ...
        def __contains__(self, key: object) -> bool: ...
        def __bool__(self) -> bool: ...
        def __len__(self) -> int: ...

else:
    from stario_cython.exchange import ParsedQuery

__all__ = ["ParsedQuery"]
