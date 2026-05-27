import io
import types

import pytest

from stario.cli.errors import CliError
from stario.cli.runtime import resolve_tracer
from stario.telemetry import JsonTracer, NoOpTracer, SqliteTracer, TTYTracer


def test_resolve_tracer_cli_wins_over_stario_tracer_env(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "noop")
    assert isinstance(resolve_tracer("json"), JsonTracer)


def test_resolve_tracer_uses_stario_tracer_when_cli_auto(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "sqlite")
    assert isinstance(resolve_tracer(None), SqliteTracer)
    assert isinstance(resolve_tracer("auto"), SqliteTracer)


def test_resolve_tracer_defaults_to_auto_tty_or_json(monkeypatch) -> None:
    monkeypatch.delenv("STARIO_TRACER", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert isinstance(resolve_tracer(None), TTYTracer)
    assert isinstance(resolve_tracer("auto"), TTYTracer)

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert isinstance(resolve_tracer(None), JsonTracer)


def test_resolve_tracer_uses_stario_tracer_env(monkeypatch) -> None:
    monkeypatch.delenv("TRACES_SQLITE", raising=False)
    monkeypatch.setenv("STARIO_TRACER", "noop")
    assert isinstance(resolve_tracer(None), NoOpTracer)


def test_resolve_tracer_cli_overrides_stario_tracer(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "noop")
    assert isinstance(resolve_tracer("json"), JsonTracer)


def test_resolve_tracer_sqlite_from_env_path(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "traces.sqlite3"
    monkeypatch.setenv("TRACES_SQLITE", str(db_path))
    tracer = resolve_tracer("sqlite")
    assert isinstance(tracer, SqliteTracer)
    assert tracer._path == db_path


def test_resolve_tracer_invalid_sqlite_env_raises_cli_error(monkeypatch) -> None:
    monkeypatch.setenv("TRACES_SQLITE_FLUSH_INTERVAL", "not-a-number")
    with pytest.raises(CliError, match="TRACES_SQLITE_FLUSH_INTERVAL"):
        resolve_tracer("sqlite")


def test_resolve_tracer_custom_invalid_return_raises(monkeypatch) -> None:
    module = types.ModuleType("bad_tracer_module")

    def factory():
        return object()

    setattr(module, "factory", factory)
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)

    with pytest.raises(CliError, match="must return a Tracer"):
        resolve_tracer("bad_tracer_module:factory")


def test_resolve_tracer_custom_valid_return(monkeypatch) -> None:
    module = types.ModuleType("good_tracer_module")

    def factory():
        return JsonTracer(output=io.StringIO())

    setattr(module, "factory", factory)
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)

    assert isinstance(resolve_tracer("good_tracer_module:factory"), JsonTracer)
