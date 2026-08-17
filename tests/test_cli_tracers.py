import sys
import types
from typing import Any, cast

import pytest

from stario.cli.env import tracer_from_env
from stario.cli.errors import CliError
from stario.telemetry.json import JsonTracer
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.tty import TTYTracer


def test_tracer_from_env_defaults_to_auto_tty_or_json(monkeypatch) -> None:
    monkeypatch.delenv("STARIO_TRACER", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert isinstance(tracer_from_env(), TTYTracer)

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert isinstance(tracer_from_env(), JsonTracer)


def test_tracer_from_env_invalid_sqlite_env_raises_cli_error(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "sqlite")
    monkeypatch.setenv("STARIO_TRACERS_SQLITE_FLUSH_INTERVAL", "not-a-number")
    with pytest.raises(CliError, match="STARIO_TRACERS_SQLITE_FLUSH_INTERVAL"):
        tracer_from_env()


def test_stario_traces_sqlite_empty_env_raises(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "sqlite")
    monkeypatch.setenv("STARIO_TRACERS_SQLITE", "   ")
    with pytest.raises(CliError, match="must not be empty"):
        tracer_from_env()


def test_tracer_from_env_custom_invalid_return_raises(monkeypatch) -> None:
    module = types.ModuleType("bad_tracer_module")

    def factory():
        return object()

    cast(Any, module).factory = factory
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)
    monkeypatch.setenv("STARIO_TRACER", "bad_tracer_module:factory")

    with pytest.raises(CliError, match="must return a Tracer"):
        tracer_from_env()


def test_tracer_from_env_module_calls_make_tracer(monkeypatch) -> None:
    module = types.ModuleType("compact_tracer_mod")

    def make_tracer():
        return NoOpTracer()

    cast(Any, module).make_tracer = make_tracer
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("STARIO_TRACER", "compact_tracer_mod")

    assert isinstance(tracer_from_env(), NoOpTracer)


def test_tracer_from_env_module_without_make_tracer_raises(monkeypatch) -> None:
    module = types.ModuleType("empty_tracer_mod")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("STARIO_TRACER", "empty_tracer_mod")

    with pytest.raises(CliError, match="has no make_tracer"):
        tracer_from_env()
