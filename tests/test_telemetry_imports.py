"""Telemetry package imports (no optional Rich dependency; tracers are normal exports)."""

import subprocess
import sys


def test_import_stario_does_not_load_rich() -> None:
    code = """
import sys
import stario
assert "rich" not in sys.modules
from stario.telemetry import Span
assert "rich" not in sys.modules
assert Span is not None
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_json_tracer_does_not_load_rich() -> None:
    code = """
import sys
from stario.telemetry import JsonTracer
import io
assert "rich" not in sys.modules
_ = JsonTracer(output=io.StringIO())
assert "rich" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_tty_tracer_module_available_without_rich() -> None:
    code = """
import sys
import stario
assert "rich" not in sys.modules
from stario.telemetry import TTYTracer
assert "rich" not in sys.modules
assert "stario.telemetry.tty" in sys.modules
_ = TTYTracer
"""
    subprocess.run([sys.executable, "-c", code], check=True)
