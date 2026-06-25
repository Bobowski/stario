"""Minimal ANSI styling when stdout is a TTY (no extra dependencies).

Used by the CLI for styled output. On Windows, styling enables VT processing once
so escape codes render in older ConHost sessions (Windows Terminal already handles them).
"""

__all__ = ["echo", "err", "report_interrupt", "style"]

import sys
from typing import TextIO

from stario._terminal import style_text


def style(
    text: str,
    *,
    bold: bool = False,
    dim: bool = False,
    fg: str | None = None,
    underline: bool = False,
    file: TextIO | None = None,
) -> str:
    """Return `text` with optional ANSI styling when `file` is a TTY.

    `fg` must be one of: cyan, yellow, green, blue, white, magenta, red.
    Disabled when `file` is not a TTY or `NO_COLOR` is set (any value).
    """
    return style_text(
        text,
        file=sys.stdout if file is None else file,
        bold=bold,
        dim=dim,
        fg=fg,
        underline=underline,
    )


def echo(*parts: str, end: str = "\n") -> None:
    print("".join(parts), end=end, flush=True)


def err(*parts: str, end: str = "\n") -> None:
    print("".join(parts), end=end, file=sys.stderr, flush=True)


def report_interrupt() -> None:
    echo()
    echo(style("Cancelled.", dim=True))
