"""Cython protocol for Stario: uvloop owns the socket; llhttp + nghttp2 parse."""

from __future__ import annotations

from typing import Any

__all__ = ["run", "serve"]


def __getattr__(name: str) -> Any:
    if name in {"run", "serve"}:
        from stario_cython.serve import run, serve

        return run if name == "run" else serve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
