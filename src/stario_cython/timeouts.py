"""Timeout cleanup mode for the Cython HTTP protocol.

Header, idle, and body-stall timeouts share one mechanism. Default is a
per-event-loop sweeper: each wake calls ``loop.time()`` once, then compares
deadlines stored on the connection / exchange.

``STARIO_CYTHON_TIMEOUTS`` (process env, read at import):

- ``sweep`` (default) — one sweeper task per ``connections`` set
- ``callback`` — per-connection ``TimerHandle`` + per-exchange stall
  ``call_later`` (kept for A/B wrk; not the long-term path)
- ``off`` / ``0`` — no header, idle, or body-stall cleanup (profiling hatch)

``STARIO_CYTHON_TIMEOUT_SWEEP`` is the sweeper period in seconds (default
``0.01``). Timeout expiry is that period at worst.
"""

from __future__ import annotations

import os

MODE_OFF = 0
MODE_CALLBACK = 1
MODE_SWEEP = 2

_SWEEP_ATTR = "_stario_timeout_sweeps"


def parse_timeout_mode(raw: str | None = None) -> int:
    if raw is None:
        raw = os.environ.get("STARIO_CYTHON_TIMEOUTS", "sweep")
    value = raw.strip().lower()
    if value in ("0", "off", "none", "false", "no"):
        return MODE_OFF
    if value in ("1", "callback", "timer", "call_later"):
        return MODE_CALLBACK
    return MODE_SWEEP


TIMEOUT_MODE = parse_timeout_mode()


def timeout_cleanup_mode() -> str:
    if TIMEOUT_MODE == MODE_OFF:
        return "off"
    if TIMEOUT_MODE == MODE_CALLBACK:
        return "callback"
    return "sweep"


def sweep_interval() -> float:
    raw = os.environ.get("STARIO_CYTHON_TIMEOUT_SWEEP", "0.01")
    try:
        value = float(raw)
    except ValueError:
        value = 0.01
    if value < 0.001:
        return 0.001
    if value > 1.0:
        return 1.0
    return value
