"""Timeout sweep for the Cython protocol.

Header, idle, and body-stall share one ``loop.time()`` compare per wake.
``STARIO_CYTHON_TIMEOUTS`` is ``sweep`` (default) or ``off``. Server's Date
tick is the wake; without Server, ``sweep_interval()`` is the fallback.
"""

from __future__ import annotations

import os

MODE_OFF = 0
MODE_SWEEP = 1

# Set on the loop while the server Date tick sweeps connection timeouts,
# so the protocol does not start its own sweeper.
DATE_TICK_SWEEP_ATTR = "_stario_date_tick_sweeps_timeouts"


def parse_timeout_mode(raw: str | None = None) -> int:
    if raw is None:
        raw = os.environ.get("STARIO_CYTHON_TIMEOUTS", "sweep")
    value = raw.strip().lower()
    if value in ("0", "off", "none", "false", "no"):
        return MODE_OFF
    return MODE_SWEEP


def timeout_cleanup_mode() -> str:
    if parse_timeout_mode() == MODE_OFF:
        return "off"
    return "sweep"


def sweep_interval() -> float:
    raw = os.environ.get("STARIO_CYTHON_TIMEOUT_SWEEP", "1")
    try:
        value = float(raw)
    except ValueError:
        value = 1.0
    if value < 0.001:
        return 0.001
    if value > 1.0:
        return 1.0
    return value
