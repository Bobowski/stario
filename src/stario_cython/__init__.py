"""Cython protocol for Stario: uvloop owns the socket, llhttp parses, App runs."""

from __future__ import annotations

from typing import Any

__all__ = ["App", "Router", "run", "serve"]


def __getattr__(name: str) -> Any:
    if name in {"run", "serve"}:
        from stario_cython.serve import run, serve

        return run if name == "run" else serve
    if name == "App":
        from stario_cython.app import App

        return App
    if name == "Router":
        from stario_cython.router import Router

        return Router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
