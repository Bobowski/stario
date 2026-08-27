"""`python -m stario_cython MODULE:bootstrap`"""

from __future__ import annotations

import argparse
import sys

from stario.cli.imports import load_symbol
from stario_cython.serve import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stario-cython")
    parser.add_argument(
        "app",
        metavar="MODULE:CALLABLE",
        help="Import path to bootstrap (async def bootstrap(app, span): ...; yield)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    bootstrap = load_symbol(args.app, label="app")
    if not callable(bootstrap):
        parser.error("app must be callable")
    run(bootstrap)  # type: ignore[arg-type]
    return 0


raise SystemExit(main())
