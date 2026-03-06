import io
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import stario.cli
from stario import CompressionConfig, Stario
from stario.cli.runtime import (
    _build_serve_command,
    default_tracer_factory,
    load_bootstrap,
    normalize_bootstrap,
    resolve_tracer_factory,
    watch_app,
)
from stario.telemetry import JsonTracer, RichTracer


def test_load_bootstrap_resolves_dotted_attribute(monkeypatch) -> None:
    module = types.ModuleType("fake_bootstrap_module")

    @asynccontextmanager
    async def bootstrap(app, span):
        yield

    module.runtime = types.SimpleNamespace(bootstrap=bootstrap)
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)

    assert callable(load_bootstrap("fake_bootstrap_module:runtime.bootstrap"))


def test_load_bootstrap_rejects_invalid_spec() -> None:
    try:
        load_bootstrap("missing-separator")
    except click.ClickException as exc:
        assert "Use 'module:callable'" in str(exc)
    else:
        raise AssertionError("Expected ClickException")


def test_load_bootstrap_imports_module_from_current_working_directory(
    monkeypatch, tmp_path: Path
) -> None:
    module_path = tmp_path / "main.py"
    module_path.write_text(
        "async def bootstrap(app, span) -> None:\n"
        "    return None\n"
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "path",
        [
            entry
            for entry in sys.path
            if Path(entry or ".").resolve() != tmp_path.resolve()
        ],
    )
    sys.modules.pop("main", None)

    try:
        assert callable(load_bootstrap("main:bootstrap"))
    finally:
        sys.modules.pop("main", None)


def test_resolve_tracer_factory_auto_prefers_rich_for_tty(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert resolve_tracer_factory(None) is RichTracer
    assert default_tracer_factory() is RichTracer


def test_resolve_tracer_factory_auto_prefers_json_for_non_tty(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert resolve_tracer_factory(None) is JsonTracer
    assert default_tracer_factory() is JsonTracer


def test_resolve_tracer_factory_custom_import(monkeypatch) -> None:
    module = types.ModuleType("fake_tracer_module")

    def tracer_factory():
        return object()

    module.runtime = types.SimpleNamespace(factory=tracer_factory)
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)

    assert resolve_tracer_factory("fake_tracer_module:runtime.factory") is tracer_factory


def test_serve_command_passes_runtime_options(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_serve_once(app_spec: str, **kwargs: object) -> None:
        calls.append({"app_spec": app_spec, **kwargs})

    monkeypatch.setattr(stario.cli, "serve_once", fake_serve_once)

    result = CliRunner().invoke(
        stario.cli.main,
        [
            "serve",
            "demo:bootstrap",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--unix-socket",
            "/tmp/stario.sock",
            "--tracer",
            "json",
            "--compress-min-size",
            "1024",
            "--compress-zstd-level",
            "-1",
            "--compress-brotli-level",
            "5",
            "--compress-gzip-level",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "app_spec": "demo:bootstrap",
            "tracer_spec": "json",
            "host": "0.0.0.0",
            "port": 9000,
            "unix_socket": "/tmp/stario.sock",
            "compression": CompressionConfig(
                min_size=1024,
                zstd_level=-1,
                brotli_level=5,
                gzip_level=7,
            ),
        }
    ]


def test_watch_command_defaults_to_current_directory(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_watch_app(app_spec: str, **kwargs: object) -> None:
        calls.append({"app_spec": app_spec, **kwargs})

    monkeypatch.setattr(stario.cli, "watch_app", fake_watch_app)

    result = CliRunner().invoke(
        stario.cli.main,
        [
            "watch",
            "demo:bootstrap",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "app_spec": "demo:bootstrap",
            "tracer_spec": None,
            "host": "127.0.0.1",
            "port": 8000,
            "unix_socket": None,
            "compression": CompressionConfig(),
            "watch_paths": (".",),
        }
    ]


def test_build_serve_command_includes_compress_flags() -> None:
    command = _build_serve_command(
        "demo:bootstrap",
        tracer_spec="json",
        host="0.0.0.0",
        port=9000,
        unix_socket="/tmp/stario.sock",
        compression=CompressionConfig(
            min_size=1024,
            zstd_level=-1,
            brotli_level=5,
            gzip_level=7,
        ),
    )

    assert command == (
        f"{__import__('sys').executable} -m stario.cli serve demo:bootstrap "
        "--host 0.0.0.0 --port 9000 --unix-socket /tmp/stario.sock "
        "--tracer json --compress-min-size 1024 --compress-zstd-level -1 "
        "--compress-brotli-level 5 --compress-gzip-level 7"
    )


def test_watch_app_uses_watchfiles_run_process(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    messages: list[str] = []

    module = types.ModuleType("watchfiles")

    def fake_run_process(*paths: str, **kwargs: object) -> None:
        calls.append((paths, kwargs))

    module.run_process = fake_run_process
    monkeypatch.setitem(__import__("sys").modules, "watchfiles", module)
    monkeypatch.setattr(
        click,
        "echo",
        lambda message="": messages.append(click.unstyle(str(message))),
    )

    watch_app(
        "demo:bootstrap",
        tracer_spec="json",
        host="0.0.0.0",
        port=9000,
        unix_socket="/tmp/stario.sock",
        compression=CompressionConfig(
            min_size=1024,
            zstd_level=-1,
            brotli_level=5,
            gzip_level=7,
        ),
        watch_paths=(".", "src"),
    )

    assert messages == ["Watching ., src for changes..."]
    assert len(calls) == 1

    paths, kwargs = calls[0]
    assert paths == (".", "src")
    assert kwargs["target"] == _build_serve_command(
        "demo:bootstrap",
        tracer_spec="json",
        host="0.0.0.0",
        port=9000,
        unix_socket="/tmp/stario.sock",
        compression=CompressionConfig(
            min_size=1024,
            zstd_level=-1,
            brotli_level=5,
            gzip_level=7,
        ),
    )
    assert kwargs["target_type"] == "command"

    callback = kwargs["callback"]
    assert callable(callback)
    callback(object())
    assert messages == [
        "Watching ., src for changes...",
        "Changes detected, reloading...",
    ]


def test_serve_command_rejects_invalid_compress_levels() -> None:
    result = CliRunner().invoke(
        stario.cli.main,
        [
            "serve",
            "demo:bootstrap",
            "--compress-gzip-level",
            "0",
        ],
    )

    assert result.exit_code != 0
    assert "--compress-gzip-level must be negative or between 1 and 9." in result.output


def test_main_help_shows_init_serve_and_watch() -> None:
    result = CliRunner().invoke(stario.cli.main, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Create, serve, and watch Stario apps." in result.output
    assert "serve" in result.output
    assert "watch" in result.output
    assert "stario serve main:bootstrap" in result.output


def test_watch_help_uses_app_language_without_toggle() -> None:
    result = CliRunner().invoke(stario.cli.main, ["watch", "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage: main watch [OPTIONS] MODULE:CALLABLE" in result.output
    assert "--watch / --no-watch" not in result.output
    assert "stario watch main:bootstrap --watch-path src" in result.output


def _test_span():
    tracer = JsonTracer(output=io.StringIO())
    return tracer.create("test.bootstrap")


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_async_context_manager() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def bootstrap(app: Stario, span):
        events.append("setup")
        yield
        events.append("teardown")

    async with normalize_bootstrap(bootstrap)(Stario(), _test_span()):
        events.append("body")

    assert events == ["setup", "body", "teardown"]


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_plain_async_function() -> None:
    events: list[str] = []

    async def bootstrap(app: Stario, span) -> None:
        events.append("setup")

    async with normalize_bootstrap(bootstrap)(Stario(), _test_span()):
        events.append("body")

    assert events == ["setup", "body"]


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_sync_function() -> None:
    events: list[str] = []

    def bootstrap(app: Stario, span) -> None:
        events.append("setup")

    async with normalize_bootstrap(bootstrap)(Stario(), _test_span()):
        events.append("body")

    assert events == ["setup", "body"]


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_undecorated_async_generator() -> None:
    events: list[str] = []

    async def bootstrap(app: Stario, span):
        events.append("setup")
        yield
        events.append("teardown")

    async with normalize_bootstrap(bootstrap)(Stario(), _test_span()):
        events.append("body")

    assert events == ["setup", "body", "teardown"]
