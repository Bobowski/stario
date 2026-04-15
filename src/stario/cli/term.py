"""Minimal ANSI styling when stdout is a TTY (no extra dependencies).

Used by ``stario init`` prompts. On Windows, ``style()`` enables VT processing once so escape
codes render in older ConHost sessions (Windows Terminal already handles them).
"""

import os
import sys
from typing import NoReturn

from stario.console import enable_windows_console_vt

_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_BLUE = "\033[34m"
_WHITE = "\033[97m"
_MAGENTA = "\033[35m"
_UNDERLINE = "\033[4m"

_FG = {
    "cyan": _CYAN,
    "yellow": _YELLOW,
    "green": _GREEN,
    "blue": _BLUE,
    "white": _WHITE,
    "magenta": _MAGENTA,
}


def style(
    text: str,
    *,
    bold: bool = False,
    dim: bool = False,
    fg: str | None = None,
    underline: bool = False,
) -> str:
    # https://no-color.org/ — any value disables color
    if "NO_COLOR" in os.environ:
        return text
    if not sys.stdout.isatty():
        return text
    if sys.platform == "win32":
        enable_windows_console_vt()
    parts: list[str] = []
    if dim:
        parts.append(_DIM)
    if bold:
        parts.append(_BOLD)
    if fg is not None and (c := _FG.get(fg)):
        parts.append(c)
    if underline:
        parts.append(_UNDERLINE)
    if not parts:
        return text
    return "".join(parts) + text + _RESET


def echo(*parts: str, end: str = "\n") -> None:
    print("".join(parts), end=end, flush=True)


def _exit_on_ctrl_c() -> NoReturn:
    echo()
    echo(style("Cancelled.", dim=True))
    raise SystemExit(130) from None


def prompt(message: str, *, default: str | None = None) -> str:
    """Prompt for input; ``message`` may include ANSI styling."""
    try:
        if default is None:
            print(f"{message}: ", end="", flush=True)
            return input().strip()
        print(f"{message} [{default}]: ", end="", flush=True)
        raw = input().strip()
        return raw or default
    except KeyboardInterrupt:
        _exit_on_ctrl_c()


def confirm(label: str, *, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            raw = input(f"{label}{suffix}: ").strip().lower()
        except KeyboardInterrupt:
            _exit_on_ctrl_c()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
