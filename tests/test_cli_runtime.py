import asyncio
import io
import shlex
import subprocess
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import stario.cli
from stario import App
from stario.cli.errors import CliError
from stario.cli.runtime import (
    _build_serve_command,
    _build_watch_filter,
    _build_watch_plan,
    _check_unix_socket_supported,
    _join_command_line_for_watchfiles,
    _resolve_cli_loop,
    _run_cli_awaitable,
    _serve_command_argv,
    default_tracer_factory,
    load_bootstrap,
    normalize_bootstrap,
    resolve_tracer_factory,
    watch_app,
)
from stario.http.writer import CompressionConfig
from stario.telemetry import JsonTracer, SqliteTracer, TTYTracer


def test_load_bootstrap_resolves_dotted_attribute(monkeypatch) -> None:
    module = types.ModuleType("fake_bootstrap_module")

    @asynccontextmanager
    async def bootstrap(app, span):
        yield

    setattr(module, "runtime", types.SimpleNamespace(bootstrap=bootstrap))
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)

    assert callable(load_bootstrap("fake_bootstrap_module:runtime.bootstrap"))


def test_load_bootstrap_rejects_invalid_spec() -> None:
    try:
        load_bootstrap("missing-separator")
    except CliError as exc:
        assert "Use 'module:callable'" in str(exc)
    else:
        raise AssertionError("Expected CliError")


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


def test_resolve_tracer_factory_auto_prefers_tty_for_tty(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert resolve_tracer_factory(None) is TTYTracer
    assert default_tracer_factory() is TTYTracer


def test_resolve_tracer_factory_auto_prefers_json_for_non_tty(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert resolve_tracer_factory(None) is JsonTracer
    assert default_tracer_factory() is JsonTracer


def test_resolve_tracer_factory_accepts_sqlite_alias() -> None:
    assert resolve_tracer_factory("sqlite") is SqliteTracer


def test_resolve_tracer_factory_accepts_tty_alias() -> None:
    assert resolve_tracer_factory("tty") is TTYTracer


def test_resolve_cli_loop_uses_uvloop_when_available(monkeypatch) -> None:
    def fake_new_event_loop() -> asyncio.AbstractEventLoop:
        raise AssertionError("fake loop factory should not be called in this test")

    fake_uvloop = types.SimpleNamespace(new_event_loop=fake_new_event_loop)
    monkeypatch.setitem(sys.modules, "uvloop", fake_uvloop)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert _resolve_cli_loop("uvloop") == ("uvloop", fake_uvloop.new_event_loop)


def test_resolve_cli_loop_rejects_uvloop_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(CliError, match="Use --loop asyncio"):
        _resolve_cli_loop("uvloop")


def test_run_cli_awaitable_uses_runner_when_loop_factory_is_available(
    monkeypatch,
) -> None:
    calls: list[object] = []

    def loop_factory() -> asyncio.AbstractEventLoop:
        raise AssertionError("fake runner should not call loop_factory")

    class FakeRunner:
        def __init__(
            self,
            *,
            loop_factory: object,
        ) -> None:
            calls.append(loop_factory)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def run(self, awaitable) -> None:
            calls.append("run")
            asyncio.run(awaitable)

    async def sample() -> None:
        calls.append("awaited")

    monkeypatch.setattr(asyncio, "Runner", FakeRunner)

    _run_cli_awaitable(sample(), loop_factory=loop_factory)

    assert calls == [loop_factory, "run", "awaited"]


def test_resolve_tracer_factory_custom_import(monkeypatch) -> None:
    module = types.ModuleType("fake_tracer_module")

    def tracer_factory():
        return object()

    setattr(module, "runtime", types.SimpleNamespace(factory=tracer_factory))
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)

    assert resolve_tracer_factory("fake_tracer_module:runtime.factory") is tracer_factory


def test_serve_command_passes_runtime_options(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_serve_once(app_spec: str, **kwargs: object) -> None:
        calls.append({"app_spec": app_spec, **kwargs})

    monkeypatch.setattr(stario.cli, "serve_once", fake_serve_once)

    code = stario.cli.main(
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
            "--compress-zstd-window-log",
            "20",
            "--compress-brotli-level",
            "5",
            "--compress-brotli-window-log",
            "21",
            "--compress-gzip-level",
            "7",
            "--compress-gzip-window-bits",
            "14",
        ],
    )

    assert code == 0
    assert calls == [
        {
            "app_spec": "demo:bootstrap",
            "tracer_spec": "json",
            "loop": "asyncio",
            "host": "0.0.0.0",
            "port": 9000,
            "unix_socket": "/tmp/stario.sock",
            "compression": CompressionConfig(
                min_size=1024,
                zstd_level=-1,
                zstd_window_log=20,
                brotli_level=5,
                brotli_window_log=21,
                gzip_level=7,
                gzip_window_bits=14,
            ),
            "max_request_header_bytes": 64 * 1024,
            "max_request_body_bytes": 10 * 1024 * 1024,
        }
    ]


def test_watch_command_defaults_to_current_directory(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_watch_app(app_spec: str, **kwargs: object) -> None:
        calls.append({"app_spec": app_spec, **kwargs})

    monkeypatch.setattr(stario.cli, "watch_app", fake_watch_app)

    code = stario.cli.main(
        [
            "watch",
            "demo:bootstrap",
        ],
    )

    assert code == 0
    assert calls == [
        {
            "app_spec": "demo:bootstrap",
            "tracer_spec": None,
            "loop": "asyncio",
            "host": "127.0.0.1",
            "port": 8000,
            "unix_socket": None,
            "compression": CompressionConfig(),
            "max_request_header_bytes": 64 * 1024,
            "max_request_body_bytes": 10 * 1024 * 1024,
            "watch_specs": (),
            "watch_ignore_specs": (),
        }
    ]


def test_watch_command_passes_watch_specs(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_watch_app(app_spec: str, **kwargs: object) -> None:
        calls.append({"app_spec": app_spec, **kwargs})

    monkeypatch.setattr(stario.cli, "watch_app", fake_watch_app)

    code = stario.cli.main(
        [
            "watch",
            "--watch",
            "app/",
            "--watch",
            "**/*.py",
            "--watch-ignore",
            "app/dev.sqlite3",
            "--watch-ignore",
            "*.wal",
            "--loop",
            "uvloop",
            "demo:bootstrap",
        ],
    )

    assert code == 0
    assert calls == [
        {
            "app_spec": "demo:bootstrap",
            "tracer_spec": None,
            "loop": "uvloop",
            "host": "127.0.0.1",
            "port": 8000,
            "unix_socket": None,
            "compression": CompressionConfig(),
            "max_request_header_bytes": 64 * 1024,
            "max_request_body_bytes": 10 * 1024 * 1024,
            "watch_specs": ("app/", "**/*.py"),
            "watch_ignore_specs": ("app/dev.sqlite3", "*.wal"),
        }
    ]


def test_build_serve_command_includes_compress_flags() -> None:
    argv = _serve_command_argv(
        "demo:bootstrap",
        loop="asyncio",
        tracer_spec="json",
        host="0.0.0.0",
        port=9000,
        unix_socket="/tmp/stario.sock",
        compression=CompressionConfig(
            min_size=1024,
            zstd_level=-1,
            zstd_window_log=20,
            brotli_level=5,
            brotli_window_log=21,
            gzip_level=7,
            gzip_window_bits=14,
        ),
        max_request_header_bytes=65536,
        max_request_body_bytes=10485760,
    )
    command = _build_serve_command(
        "demo:bootstrap",
        loop="asyncio",
        tracer_spec="json",
        host="0.0.0.0",
        port=9000,
        unix_socket="/tmp/stario.sock",
        compression=CompressionConfig(
            min_size=1024,
            zstd_level=-1,
            zstd_window_log=20,
            brotli_level=5,
            brotli_window_log=21,
            gzip_level=7,
            gzip_window_bits=14,
        ),
        max_request_header_bytes=65536,
        max_request_body_bytes=10485760,
    )
    assert command == _join_command_line_for_watchfiles(argv)


def test_join_command_line_matches_watchfiles_on_windows(monkeypatch) -> None:
    """Windows: ``subprocess.list2cmdline`` so watchfiles' ``split_cmd`` can parse the string."""
    monkeypatch.setattr(sys, "platform", "win32")
    argv = [
        r"C:\Program Files\Python\python.exe",
        "-m",
        "stario.cli",
        "serve",
        "app:bootstrap",
        "--port",
        "8000",
    ]
    joined = _join_command_line_for_watchfiles(argv)
    assert joined == subprocess.list2cmdline(argv)


def test_join_command_line_matches_watchfiles_on_posix(monkeypatch) -> None:
    """POSIX: ``shlex.join`` matches watchfiles' ``shlex.split(..., posix=True)``."""
    monkeypatch.setattr(sys, "platform", "linux")
    argv = [
        "/usr/bin/python3",
        "-m",
        "stario.cli",
        "serve",
        "app:bootstrap",
        "--port",
        "8000",
    ]
    joined = _join_command_line_for_watchfiles(argv)
    assert joined == shlex.join(argv)


def test_unix_socket_not_supported_raises(monkeypatch) -> None:
    import stario.cli.runtime as rt

    class _NoAfUnix:
        pass

    monkeypatch.setattr(rt, "socket", _NoAfUnix())
    with pytest.raises(CliError, match="Unix domain sockets"):
        _check_unix_socket_supported("/tmp/x.sock")


def test_watch_app_uses_watchfiles_run_process(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    messages: list[str] = []

    module = types.ModuleType("watchfiles")
    filters_module = types.ModuleType("watchfiles.filters")
    from watchfiles.filters import DefaultFilter

    def fake_run_process(*paths: str, **kwargs: object) -> None:
        calls.append((paths, kwargs))

    setattr(module, "run_process", fake_run_process)
    monkeypatch.setitem(__import__("sys").modules, "watchfiles", module)
    setattr(filters_module, "DefaultFilter", DefaultFilter)
    monkeypatch.setitem(__import__("sys").modules, "watchfiles.filters", filters_module)
    monkeypatch.setattr(
        "stario.cli.runtime._watch_cli_line",
        lambda msg: messages.append(msg),
    )

    watch_app(
        "demo:bootstrap",
        loop="asyncio",
        tracer_spec="json",
        host="0.0.0.0",
        port=9000,
        unix_socket="/tmp/stario.sock",
        compression=CompressionConfig(
            min_size=1024,
            zstd_level=-1,
            zstd_window_log=20,
            brotli_level=5,
            brotli_window_log=21,
            gzip_level=7,
            gzip_window_bits=14,
        ),
        max_request_header_bytes=65536,
        max_request_body_bytes=10485760,
        watch_specs=("src/",),
    )

    assert messages == ["Watching src for changes..."]
    assert len(calls) == 1

    paths, kwargs = calls[0]
    assert paths == ("src",)
    assert kwargs["target"] == _build_serve_command(
        "demo:bootstrap",
        loop="asyncio",
        tracer_spec="json",
        host="0.0.0.0",
        port=9000,
        unix_socket="/tmp/stario.sock",
        compression=CompressionConfig(
            min_size=1024,
            zstd_level=-1,
            zstd_window_log=20,
            brotli_level=5,
            brotli_window_log=21,
            gzip_level=7,
            gzip_window_bits=14,
        ),
        max_request_header_bytes=65536,
        max_request_body_bytes=10485760,
    )
    assert kwargs["target_type"] == "command"
    watch_filter = kwargs["watch_filter"]
    assert callable(watch_filter)
    assert watch_filter(object(), str(Path.cwd().resolve() / "src" / "app.py"))
    assert not watch_filter(object(), str(Path.cwd().resolve() / "docs" / "index.py"))
    assert not watch_filter(object(), str(Path.cwd().resolve() / "dev.sqlite3"))
    assert not watch_filter(object(), str(Path.cwd().resolve() / "dev.sqlite3-wal"))
    assert not watch_filter(object(), str(Path.cwd().resolve() / "app.py"))

    callback = kwargs["callback"]
    assert callable(callback)
    callback(object())
    assert messages == [
        "Watching src for changes...",
        "Changes detected, reloading...",
    ]


def test_build_watch_plan_parses_files_directories_and_globs() -> None:
    plan = _build_watch_plan(
        watch_specs=("src/", "main.py", "**/*.py"),
        ignore_specs=("tests/", "tmp/dev.sqlite3", "*.wal"),
    )

    assert plan.roots == ("src", ".",)
    assert tuple(spec.recursive for spec in plan.include_paths) == (True, False)
    assert plan.include_globs == ("**/*.py",)
    assert tuple(spec.recursive for spec in plan.ignore_paths) == (True, False)
    assert "*.wal" in plan.ignore_globs
    assert "*.sqlite3" in plan.ignore_globs


def test_build_watch_filter_applies_watch_and_ignore_specs() -> None:
    watch_filter = _build_watch_filter(
        _build_watch_plan(
            watch_specs=("src/", "**/*.py"),
            ignore_specs=("tests/", "var/dev.sqlite3", "*.wal"),
        )
    )

    cwd = Path.cwd().resolve()

    assert watch_filter(object(), str(cwd / "src" / "stario" / "cli" / "runtime.py"))
    assert not watch_filter(object(), str(cwd / "docs" / "readme.md"))
    assert not watch_filter(object(), str(cwd / "tests" / "test_cli_runtime.py"))
    assert not watch_filter(object(), str(cwd / "var" / "dev.sqlite3"))
    assert not watch_filter(object(), str(cwd / "var" / "db.wal"))
    assert not watch_filter(object(), str(cwd / "src" / "stario" / "db.sqlite3"))


def test_serve_command_rejects_invalid_compress_levels(capsys) -> None:
    code = stario.cli.main(
        [
            "serve",
            "demo:bootstrap",
            "--compress-gzip-level",
            "0",
        ],
    )

    assert code != 0
    err = capsys.readouterr().err
    assert "--compress-gzip-level must be negative or between 1 and 9." in err


def test_serve_command_rejects_invalid_window_bits(capsys) -> None:
    code = stario.cli.main(
        [
            "serve",
            "demo:bootstrap",
            "--compress-brotli-window-log",
            "25",
        ],
    )

    assert code != 0
    err = capsys.readouterr().err
    assert "--compress-brotli-window-log must be between 10 and 24." in err


def test_main_help_shows_init_serve_and_watch(capsys) -> None:
    code = stario.cli.main(["--help"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Create, serve, and watch App apps." in out
    assert "serve" in out
    assert "watch" in out
    assert "stario serve main:bootstrap" in out


def test_watch_help_uses_app_language_without_toggle(capsys) -> None:
    code = stario.cli.main(["watch", "--help"])

    assert code == 0
    out = capsys.readouterr().out
    assert "usage: stario watch" in out.lower()
    assert "module:callable" in out.lower()
    assert "--watch / --no-watch" not in out
    assert "--loop" in out and "{asyncio,uvloop}" in out.replace(" ", "")
    assert "stario watch main:bootstrap --watch app/" in out
    assert "--watch SPEC" in out
    assert "--watch-ignore SPEC" in out


def _test_span():
    tracer = JsonTracer(output=io.StringIO())
    return tracer.create("test.bootstrap")


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_async_context_manager() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def bootstrap(app: App, span):
        events.append("setup")
        yield
        events.append("teardown")

    async with normalize_bootstrap(bootstrap)(App(), _test_span()):
        events.append("body")

    assert events == ["setup", "body", "teardown"]


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_plain_async_function() -> None:
    events: list[str] = []

    async def bootstrap(app: App, span) -> None:
        events.append("setup")

    async with normalize_bootstrap(bootstrap)(App(), _test_span()):
        events.append("body")

    assert events == ["setup", "body"]


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_sync_function() -> None:
    events: list[str] = []

    def bootstrap(app: App, span) -> None:
        events.append("setup")

    async with normalize_bootstrap(bootstrap)(App(), _test_span()):
        events.append("body")

    assert events == ["setup", "body"]


@pytest.mark.asyncio
async def test_normalize_bootstrap_accepts_undecorated_async_generator() -> None:
    events: list[str] = []

    async def bootstrap(app: App, span):
        events.append("setup")
        yield
        events.append("teardown")

    async with normalize_bootstrap(bootstrap)(App(), _test_span()):
        events.append("body")

    assert events == ["setup", "body", "teardown"]
