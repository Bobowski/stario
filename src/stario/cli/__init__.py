"""Stario command-line interface.

Entry point: `stario.cli.main:main` (also `python -m stario.cli`).

The public API is `main` in `stario.cli.main`.
Internal modules (`env`, `runtime`, `errors`, and so on) are CLI
implementation details — import them directly in tests, not from this package
root. Prefer the installed `stario` command; use `python -m stario.cli` when
no console script is on `PATH`.
"""
