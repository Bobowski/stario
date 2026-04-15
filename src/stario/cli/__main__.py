"""Supports ``python -m stario.cli`` for environments that invoke the package without an installed entry point."""

from . import main

if __name__ == "__main__":
    raise SystemExit(main())
