"""Cython protocol for Stario: uvloop owns the socket; llhttp + nghttp2 parse."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stario_cython.serve import run, serve

# The extensions import stario at module init and stario imports them back;
# loading stario first keeps `import stario_cython.exchange` order-independent.
import stario  # noqa: F401  # pyright: ignore[reportUnusedImport]

__all__ = ["run", "serve"]


def __getattr__(name: str) -> Any:
    if name in {"run", "serve"}:
        from stario_cython.serve import run, serve

        return run if name == "run" else serve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
