"""Stario CLI entry point: argparse wiring and command dispatch."""

import argparse
import sys

from stario import __version__
from stario.cli.errors import CliError
from stario.cli.help import ROOT_EPILOG, SERVE_EPILOG, WATCH_EPILOG
from stario.cli.runtime import serve_once

from . import term


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stario",
        description="Serve and watch Stario apps.",
        epilog=ROOT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser(
        "serve",
        help="Start a Stario app once.",
        description="Start a Stario app once.",
        epilog=SERVE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve_p.add_argument(
        "app",
        metavar="MODULE:CALLABLE",
        help="Import path to the app bootstrap callable (dotted attributes allowed)",
    )

    watch_p = sub.add_parser(
        "watch",
        help="Start a Stario app and restart it on changes.",
        description="Start a Stario app and restart it on changes.",
        epilog=WATCH_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    watch_p.add_argument(
        "--watch",
        dest="watch_specs",
        action="append",
        default=[],
        metavar="SPEC",
        help="File or directory to watch recursively (default: current directory when omitted)",
    )
    watch_p.add_argument(
        "--watch-ignore",
        dest="watch_ignore_specs",
        action="append",
        default=[],
        metavar="SPEC",
        help="File, directory, or filename glob to ignore; sqlite files are ignored by default",
    )
    watch_p.add_argument(
        "app",
        metavar="MODULE:CALLABLE",
        help="Import path to the app bootstrap callable (dotted attributes allowed)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the Stario CLI and return a process exit code.

    `argv` defaults to `sys.argv[1:]`. Returns `0` on success, `1` for
    `CliError`, `2` for argparse usage errors, and
    `130` after `KeyboardInterrupt`. Unexpected exceptions propagate.
    """
    argv = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1

    try:
        match args.command:
            case "serve":
                serve_once(args.app)
            case "watch":
                from stario.cli.runtime import watch_app

                watch_app(
                    args.app,
                    watch_specs=tuple(args.watch_specs),
                    watch_ignore_specs=tuple(args.watch_ignore_specs),
                )
            case _:
                pass
    except CliError as e:
        term.err(str(e))
        return 1
    except KeyboardInterrupt:
        term.report_interrupt()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
