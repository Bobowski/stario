"""Marker for expected CLI failures.

Caught at the CLI entry boundary (`main`). Use
`CliError` for invalid user input, configuration, and imports.
Framework/runtime bugs should use `StarioError` and
are translated to `CliError` at the CLI boundary where appropriate.
"""

from typing import NoReturn

__all__ = ["CliError", "report"]


class CliError(Exception):
    """User-facing CLI failure (contrast with unexpected tracebacks).

    Raised for invalid specs, env vars, and watch paths. Caught by `main()`,
    which prints the message on stderr and returns exit code `1`. Argparse
    usage errors return `2` without raising `CliError`.
    """


def report(exc: CliError, /) -> NoReturn:
    """Print `exc` on stderr and exit with status 1."""
    from stario.cli.term import err

    err(str(exc))
    raise SystemExit(1) from None
