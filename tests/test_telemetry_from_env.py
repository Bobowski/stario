import io

import pytest

from stario.telemetry import JsonTracer, NoOpTracer, SqliteTracer, TTYTracer


def test_sqlite_tracer_from_env_defaults(monkeypatch) -> None:
    for key in (
        "TRACES_SQLITE",
        "TRACES_SQLITE_FLUSH_INTERVAL",
        "TRACES_SQLITE_MAX_PENDING_SPANS",
        "TRACES_SQLITE_MAX_BATCH_SPANS",
    ):
        monkeypatch.delenv(key, raising=False)

    tracer = SqliteTracer.from_env()
    assert tracer._path.as_posix() == "stario-traces.sqlite3"
    assert tracer._flush_interval == 0.125
    assert tracer._max_pending_spans == 65536
    assert tracer._wake_batch_spans == 512


def test_sqlite_tracer_from_env_overrides_when_set(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("TRACES_SQLITE", str(db_path))
    monkeypatch.setenv("TRACES_SQLITE_FLUSH_INTERVAL", "0.25")
    monkeypatch.setenv("TRACES_SQLITE_MAX_PENDING_SPANS", "1024")
    monkeypatch.setenv("TRACES_SQLITE_MAX_BATCH_SPANS", "64")

    tracer = SqliteTracer.from_env()
    assert tracer._path == db_path
    assert tracer._flush_interval == 0.25
    assert tracer._max_pending_spans == 1024
    assert tracer._wake_batch_spans == 64


def test_sqlite_tracer_from_env_honors_zero_when_explicit(monkeypatch) -> None:
    monkeypatch.setenv("TRACES_SQLITE_FLUSH_INTERVAL", "0")
    with pytest.raises(ValueError, match="flush_interval"):
        SqliteTracer.from_env()


def test_sqlite_tracer_from_env_rejects_invalid_number(monkeypatch) -> None:
    monkeypatch.setenv("TRACES_SQLITE_MAX_BATCH_SPANS", "nope")
    with pytest.raises(ValueError, match="TRACES_SQLITE_MAX_BATCH_SPANS"):
        SqliteTracer.from_env()


def test_json_tracer_from_env_defaults(monkeypatch) -> None:
    for key in (
        "TRACES_JSON_FLUSH_INTERVAL",
        "TRACES_JSON_MAX_PENDING_SPANS",
        "TRACES_JSON_MAX_BATCH_SPANS",
    ):
        monkeypatch.delenv(key, raising=False)

    tracer = JsonTracer.from_env()
    assert tracer._flush_interval == 0.125
    assert tracer._max_pending_spans == 65536
    assert tracer._wake_batch_spans == 512


def test_json_tracer_from_env_overrides_when_set(monkeypatch) -> None:
    monkeypatch.setenv("TRACES_JSON_FLUSH_INTERVAL", "0.5")
    monkeypatch.setenv("TRACES_JSON_MAX_PENDING_SPANS", "2048")
    monkeypatch.setenv("TRACES_JSON_MAX_BATCH_SPANS", "128")

    tracer = JsonTracer.from_env()
    assert tracer._flush_interval == 0.5
    assert tracer._max_pending_spans == 2048
    assert tracer._wake_batch_spans == 128


def test_json_tracer_from_env_custom_output_still_uses_stdout_by_default() -> None:
    tracer = JsonTracer.from_env()
    assert tracer._output is not None


def test_tty_tracer_from_env_returns_instance() -> None:
    assert isinstance(TTYTracer.from_env(), TTYTracer)


def test_noop_tracer_from_env_returns_instance() -> None:
    assert isinstance(NoOpTracer.from_env(), NoOpTracer)


def test_json_tracer_direct_constructor_unaffected() -> None:
    output = io.StringIO()
    tracer = JsonTracer(output=output, flush_interval=0.2)
    assert tracer._output is output
    assert tracer._flush_interval == 0.2
