"""Supports `python -m stario.cli` for environments without an installed entry point.

Prefer the installed `stario` command when available.
"""

from stario.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
