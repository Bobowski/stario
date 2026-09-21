"""Timeout cleanup for the Cython HTTP protocol.

Header, idle, and body-stall timeouts share **one** mechanism: compare
deadlines stored on the connection to ``loop.time()`` computed once per
wake.

Under ``stario.http.server.Server`` that wake is the Date-header tick
(once a second). Header/idle/body-stall defaults are 5s/5s/30s, so 1s
granularity is enough and adds no extra timer. Protocols constructed
without Server (tests, raw ``create_server``) start a fallback sweeper
with the same period.

``STARIO_CYTHON_TIMEOUTS`` (process env, read at import):

- ``sweep`` (default)
- ``off`` / ``0`` — no header, idle, or body-stall cleanup (profiling hatch)

``STARIO_CYTHON_TIMEOUT_SWEEP`` overrides the fallback sweeper period
(default ``1``). Tests set ``0.05`` so slowloris cases finish quickly.
"""

from __future__ import annotations

import os

MODE_OFF = 0
MODE_SWEEP = 1

# Keep in sync with ``stario.http.server._DATE_TICK_SWEEPS_TIMEOUTS``.
DATE_TICK_SWEEP_ATTR = "_stario_date_tick_sweeps_timeouts"


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
