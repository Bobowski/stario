"""Timeout cleanup for the Cython HTTP protocol.

Header, idle, and body-stall timeouts share **one** mechanism: a sweeper
over the connection set. Each wake calls ``loop.time()`` once, then
compares deadlines stored on the protocol / exchange.

``STARIO_CYTHON_TIMEOUTS`` (process env, read at import):

- ``sweep`` (default) — one sweeper task per ``connections`` set
- ``off`` / ``0`` — no header, idle, or body-stall cleanup (profiling hatch)

``STARIO_CYTHON_TIMEOUT_SWEEP`` is the sweeper period in seconds (default
``0.05``). Expiry is that period at worst. 10ms was measurable on 2MB
streaming wrk; 50ms is still tight for the 5s/30s production timeouts.
"""

from __future__ import annotations

import os

MODE_OFF = 0
MODE_SWEEP = 1

_SWEEP_ATTR = "_stario_timeout_sweeps"


def parse_timeout_mode(raw: str | None = None) -> int:
    if raw is None:
        raw = os.environ.get("STARIO_CYTHON_TIMEOUTS", "sweep")
    value = raw.strip().lower()
    if value in ("0", "off", "none", "false", "no"):
        return MODE_OFF
    return MODE_SWEEP


TIMEOUT_MODE = parse_timeout_mode()


def timeout_cleanup_mode() -> str:
    if TIMEOUT_MODE == MODE_OFF:
        return "off"
    return "sweep"


def sweep_interval() -> float:
    raw = os.environ.get("STARIO_CYTHON_TIMEOUT_SWEEP", "0.05")
    try:
        value = float(raw)
    except ValueError:
        value = 0.05
    if value < 0.001:
        return 0.001
    if value > 1.0:
        return 1.0
    return value
