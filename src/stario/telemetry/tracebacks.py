"""Format recorded exceptions for telemetry sinks (TTY, JSON, SQLite).

The ``traceback`` module does not omit “framework” frames by policy; the usual
approach is to build a ``traceback.StackSummary`` from a filtered walk of
``exc.__traceback__``. Here we skip frames whose file path lies under the installed
``stario`` package so logs show **application** code first, similar to hiding
site-packages in some consoles.
"""

from functools import lru_cache
from pathlib import Path
from traceback import StackSummary, format_exception_only, format_list
from types import TracebackType


@lru_cache(maxsize=1)
def _stario_install_root() -> Path:
    import stario

    return Path(stario.__file__).resolve().parent


def _frame_is_under_stario(filename: str) -> bool:
    if filename.startswith("<"):
        return False
    try:
        return Path(filename).resolve().is_relative_to(_stario_install_root())
    except ValueError:
        return False


def _walk_tb_skip_stario(tb: TracebackType | None):
    while tb is not None:
        fn = tb.tb_frame.f_code.co_filename
        if not _frame_is_under_stario(fn):
            yield tb.tb_frame, tb.tb_lineno
        tb = tb.tb_next


def format_exception_for_telemetry(exc: BaseException) -> str:
    """Format *exc* with traceback lines for non-``stario`` frames only.

    If there is no traceback, or every frame is inside ``stario``, emit the exception
    type and message only (no ``Traceback (most recent call last)`` block).
    """
    tb = exc.__traceback__
    if tb is None:
        return "".join(format_exception_only(type(exc), exc))
    w = tb
    while w is not None:
        if not _frame_is_under_stario(w.tb_frame.f_code.co_filename):
            break
        w = w.tb_next
    else:
        return "".join(format_exception_only(type(exc), exc))
    stack = StackSummary.extract(_walk_tb_skip_stario(tb))
    parts: list[str] = []
    if stack:
        parts.append("Traceback (most recent call last):\n")
        parts.extend(format_list(stack))
    parts.extend(format_exception_only(type(exc), exc))
    return "".join(parts)
