"""Telemetry package imports (no optional Rich dependency; tracers are normal exports)."""

import subprocess
import sys


def test_import_stario_does_not_load_rich() -> None:
    code = """
import sys
import stario
assert "rich" not in sys.modules
assert "stario.telemetry.json" not in sys.modules
assert "stario.telemetry.sqlite" not in sys.modules
assert "stario.telemetry.tty" not in sys.modules
assert stario.Span is not None
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_tty_tracer_module_available_without_rich() -> None:
    code = """
import sys
import stario
assert "rich" not in sys.modules
from stario.telemetry.tty import TTYTracer
assert "rich" not in sys.modules
assert "stario.telemetry.tty" in sys.modules
_ = TTYTracer
"""
    subprocess.run([sys.executable, "-c", code], check=True)
