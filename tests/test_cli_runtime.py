"""Tests for watch CLI flags and validation."""

import shlex
import sys
from typing import cast

import pytest

from stario.cli.errors import CliError
from stario.cli.main import main
from stario.cli.runtime import watch_app


def _expected_watch_serve_command(app_spec: str) -> str:
    return " ".join(
        shlex.quote(part)
        for part in (sys.executable, "-m", "stario.cli", "serve", app_spec)
    )


def test_watch_command_defaults_to_current_directory(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_watch_app(app_spec: str, **kwargs: object) -> None:
        calls.append({"app_spec": app_spec, **kwargs})

    monkeypatch.setattr("stario.cli.runtime.watch_app", fake_watch_app)

    code = main(["watch", "demo:bootstrap"])

    assert code == 0
    assert calls == [
        {
            "app_spec": "demo:bootstrap",
            "watch_specs": (),
            "watch_ignore_specs": (),
        }
    ]


def test_watch_command_passes_watch_specs(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_watch_app(app_spec: str, **kwargs: object) -> None:
        calls.append({"app_spec": app_spec, **kwargs})

    monkeypatch.setattr("stario.cli.runtime.watch_app", fake_watch_app)

    code = main(
        [
            "watch",
            "--watch",
            "app/",
            "--watch-ignore",
            "*.log",
            "demo:bootstrap",
        ],
    )

    assert code == 0
    assert calls == [
        {
            "app_spec": "demo:bootstrap",
            "watch_specs": ("app/",),
            "watch_ignore_specs": ("*.log",),
        }
    ]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"watch_specs": ("missing-dir/",)}, "path does not exist"),
        (
            {"watch_specs": (), "watch_ignore_specs": ("missing-dir/",)},
            "path does not exist",
        ),
        ({"watch_specs": ("**/*.py",)}, "expects paths, not glob patterns"),
        (
            {"watch_specs": (), "watch_ignore_specs": ("data/*.db",)},
            "simple filename globs only",
        ),
        ({"watch_specs": (" ",)}, "entries cannot be empty"),
    ],
)
def test_watch_app_rejects_invalid_specs(monkeypatch, kwargs, match) -> None:
    def fail_if_started(*args: object, **kwargs: object) -> None:
        raise AssertionError("watch process should not start")

    monkeypatch.setattr("watchfiles.run_process", fail_if_started)

    with pytest.raises(CliError, match=match):
        watch_app("demo:bootstrap", **kwargs)


def test_watch_app_passes_sigint_timeout_and_serve_command(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: list[dict[str, object]] = []

    def fake_run_process(*args: object, **kwargs: object) -> int:
        captured.append(kwargs)
        return 0

    monkeypatch.setattr("watchfiles.run_process", fake_run_process)
    monkeypatch.setenv("STARIO_GRACEFUL_SHUTDOWN_TIMEOUT", "12.5")

    watch_app("demo:bootstrap", watch_specs=())

    kwargs = captured[0]
    command = kwargs["target"]
    assert kwargs["sigint_timeout"] == 14
    assert kwargs["target_type"] == "command"
    assert command == _expected_watch_serve_command("demo:bootstrap")
    parts = shlex.split(cast(str, command))
    assert parts[:4] == [sys.executable, "-m", "stario.cli", "serve"]
    assert parts[4] == "demo:bootstrap"
