"""Tests for STARIO_TRACER / STARIO_* environment configuration."""

import json
import sys
import types
from typing import Any, cast

import pytest

from stario.exceptions import StarioError
from stario.http.config import server_config_from_env
from stario.telemetry import tracer_from_env
from stario.telemetry.json import JsonTracer
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.sqlite import SqliteTracer
from stario.telemetry.tty import TTYTracer


def test_server_config_from_env_reads_overrides(monkeypatch) -> None:
    monkeypatch.delenv("STARIO_SSL_CERTFILE", raising=False)
    monkeypatch.delenv("STARIO_SSL_KEYFILE", raising=False)
    monkeypatch.setenv("STARIO_HOST", "0.0.0.0")
    monkeypatch.setenv("STARIO_PORT", "9000")
    monkeypatch.setenv("STARIO_UNIX_SOCKET", "/tmp/stario.sock")
    monkeypatch.setenv("STARIO_GRACEFUL_SHUTDOWN_TIMEOUT", "12.5")
    monkeypatch.setenv("STARIO_REUSE_ADDR", "0")
    monkeypatch.setenv("STARIO_THREADS", "4")

    config = server_config_from_env()
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.unix_socket == "/tmp/stario.sock"
    assert config.graceful_shutdown_timeout == 12.5
    assert config.reuse_addr is False
    assert config.ssl is None
    assert config.threads == 4


@pytest.mark.parametrize(
    ("env_name", "value", "fragment"),
    [
        ("STARIO_PORT", "70000", "port must be between"),
        ("STARIO_THREADS", "0", "threads must be at least 1"),
        ("STARIO_COMPRESS_ZSTD_LEVEL", "23", "zstd_level"),
        (
            "STARIO_REQUESTS_HEADER_TIMEOUT",
            "0",
            "header_timeout must be greater than 0",
        ),
        ("STARIO_REUSE_ADDR", "maybe", "STARIO_REUSE_ADDR"),
    ],
)
def test_invalid_server_env_raises(monkeypatch, env_name, value, fragment) -> None:
    monkeypatch.setenv(env_name, value)
    with pytest.raises(StarioError, match=fragment):
        server_config_from_env()


def test_tracer_from_env_defaults_to_auto_tty_or_json(monkeypatch) -> None:
    monkeypatch.delenv("STARIO_TRACER", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert isinstance(tracer_from_env(), TTYTracer)

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert isinstance(tracer_from_env(), JsonTracer)


def test_tracer_from_env_explicit_builtins(monkeypatch) -> None:
    for value, cls in (
        ("tty", TTYTracer),
        ("json", JsonTracer),
        ("noop", NoOpTracer),
    ):
        monkeypatch.setenv("STARIO_TRACER", value)
        assert isinstance(tracer_from_env(), cls)


def test_tracer_from_env_tty_ascii(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "tty")
    monkeypatch.delenv("STARIO_TRACERS_TTY_ASCII", raising=False)
    tracer = tracer_from_env()
    assert isinstance(tracer, TTYTracer)
    assert tracer._ascii is (tracer._write_enc is not None)

    monkeypatch.setenv("STARIO_TRACERS_TTY_ASCII", "1")
    tracer = tracer_from_env()
    assert isinstance(tracer, TTYTracer)
    assert tracer._ascii is True

    monkeypatch.setenv("STARIO_TRACERS_TTY_ASCII", "0")
    tracer = tracer_from_env()
    assert isinstance(tracer, TTYTracer)
    assert tracer._ascii is False

    monkeypatch.setenv("STARIO_TRACERS_TTY_ASCII", "maybe")
    with pytest.raises(StarioError, match="STARIO_TRACERS_TTY_ASCII"):
        tracer_from_env()


def test_tracer_from_env_json_writes_to_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STARIO_TRACER", "json")
    path = tmp_path / "spans.ndjson"
    monkeypatch.setenv("STARIO_TRACERS_JSON", str(path))

    tracer = tracer_from_env()
    assert isinstance(tracer, JsonTracer)
    with tracer, tracer.create("request"):
        pass

    assert json.loads(path.read_text())["name"] == "request"


def test_tracer_from_env_json_defaults_to_stdout(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "json")
    monkeypatch.delenv("STARIO_TRACERS_JSON", raising=False)
    tracer = tracer_from_env()
    assert isinstance(tracer, JsonTracer)
    assert tracer._path is None

    monkeypatch.setenv("STARIO_TRACERS_JSON", "   ")
    tracer = tracer_from_env()
    assert isinstance(tracer, JsonTracer)
    assert tracer._path is None


def test_tracer_from_env_sqlite_is_deprecated(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("STARIO_TRACER", "sqlite")
    monkeypatch.setenv("STARIO_TRACERS_SQLITE", str(tmp_path / "traces.sqlite3"))

    tracer = tracer_from_env()

    assert isinstance(tracer, SqliteTracer)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "[stario] STARIO_TRACER=sqlite is deprecated: set STARIO_TRACER=json "
        "and STARIO_TRACERS_JSON=<path>. Removed in 6.0.\n"
    )


def test_tracer_from_env_invalid_sqlite_env_raises(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "sqlite")
    monkeypatch.setenv("STARIO_TRACERS_SQLITE_FLUSH_INTERVAL", "not-a-number")
    with pytest.raises(StarioError, match="STARIO_TRACERS_SQLITE_FLUSH_INTERVAL"):
        tracer_from_env()


def test_stario_traces_sqlite_empty_env_raises(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "sqlite")
    monkeypatch.setenv("STARIO_TRACERS_SQLITE", "   ")
    with pytest.raises(StarioError, match="must not be empty"):
        tracer_from_env()


def test_tracer_from_env_custom_invalid_return_raises(monkeypatch) -> None:
    module = types.ModuleType("bad_tracer_module")

    def factory():
        return object()

    cast(Any, module).factory = factory
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)
    monkeypatch.setenv("STARIO_TRACER", "bad_tracer_module:factory")

    with pytest.raises(StarioError, match="must return a Tracer"):
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

    with pytest.raises(StarioError, match="has no make_tracer"):
        tracer_from_env()


def test_tracer_from_env_unimportable_module_raises(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_TRACER", "no_such_tracer_module_xyz")
    with pytest.raises(StarioError, match="importing 'no_such_tracer_module_xyz'"):
        tracer_from_env()
