"""CLI helpers: import ``module:bootstrap``, resolve tracers, serve/watch.

Bootstrap normalization lives in ``stario.http.bootstrap``. In tests use
``async with stario.testing.TestClient(bootstrap)`` (pytest-asyncio).
"""

import asyncio
import importlib
import shlex
import socket
import subprocess
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal, cast

from stario.cli.errors import CliError
from stario.cli.term import style as _cli_style
from stario.http.bootstrap import BootstrapCandidate, normalize_bootstrap
from stario.http.server import AppBootstrap, Server
from stario.http.writer import CompressionConfig
from stario.telemetry.core import Tracer
from stario.telemetry.json import JsonTracer
from stario.telemetry.sqlite import SqliteTracer
from stario.telemetry.tty import TTYTracer

type TracerFactory = Callable[[], Tracer]
type CliLoop = Literal["asyncio", "uvloop"]


def _watch_cli_line(msg: str) -> None:
    """Watch-mode status line. Kept as a module-level function so tests can monkeypatch it."""
    print(msg, flush=True)


WATCH_GLOB_CHARS = frozenset("*?[")
DEFAULT_WATCH_IGNORE_SPECS = (
    "*.sqlite3",
    "*.sqlite3-*",
    "*.sqlite",
    "*.sqlite-*",
    "*.db",
    "*.db-*",
)


@dataclass(frozen=True, slots=True)
class _WatchPathSpec:
    path: Path
    recursive: bool


@dataclass(frozen=True, slots=True)
class _WatchPlan:
    roots: tuple[str, ...]
    include_paths: tuple[_WatchPathSpec, ...]
    include_globs: tuple[str, ...]
    ignore_paths: tuple[_WatchPathSpec, ...]
    ignore_globs: tuple[str, ...]


def _ensure_cwd_on_syspath() -> None:
    cwd = Path.cwd().resolve()
    for entry in sys.path:
        try:
            candidate = cwd if entry == "" else Path(entry).resolve()
        except OSError:
            continue
        if candidate == cwd:
            return

    # Console-script entry points can omit the app directory from sys.path.
    sys.path.insert(0, str(cwd))


def _load_symbol(spec: str, *, label: str) -> object:
    module_name, separator, attr_path = spec.partition(":")
    if not separator or not module_name or not attr_path:
        raise CliError(f"Invalid {label} '{spec}'. Use 'module:callable'.")

    _ensure_cwd_on_syspath()

    try:
        current: object = importlib.import_module(module_name)
    except Exception as exc:
        raise CliError(
            f"Could not import module '{module_name}' for {label} '{spec}': {exc}"
        ) from exc

    for part in attr_path.split("."):
        try:
            current = getattr(current, part)
        except AttributeError as exc:
            raise CliError(
                f"Module '{module_name}' does not define '{attr_path}' for {label} '{spec}'."
            ) from exc

    return current


def load_bootstrap(spec: str) -> AppBootstrap:
    """Import ``module:callable`` and wrap it with ``normalize_bootstrap`` for ``Server``."""
    bootstrap = _load_symbol(spec, label="app")
    if not callable(bootstrap):
        raise CliError(f"App '{spec}' must be callable.")
    return normalize_bootstrap(cast(BootstrapCandidate, bootstrap))


def default_tracer_factory() -> TracerFactory:
    """TTY ⇒ ``TTYTracer``, else ``JsonTracer`` (used when ``--tracer auto``)."""
    return TTYTracer if sys.stdout.isatty() else JsonTracer


def resolve_tracer_factory(spec: str | None) -> TracerFactory:
    """Map ``auto``/``tty``/``json``/``sqlite`` or ``module:factory`` to a zero-arg tracer constructor."""
    if spec in (None, "", "auto"):
        return default_tracer_factory()
    if spec == "tty":
        return TTYTracer
    if spec == "json":
        return JsonTracer
    if spec == "sqlite":
        return SqliteTracer

    factory = _load_symbol(spec, label="telemetry output")
    if not callable(factory):
        raise CliError(f"Telemetry output '{spec}' must be callable.")
    return cast(TracerFactory, factory)


def _resolve_cli_loop(
    loop: CliLoop,
) -> tuple[CliLoop, Callable[[], asyncio.AbstractEventLoop] | None]:
    if loop == "asyncio":
        return loop, None

    if sys.platform == "win32":
        raise CliError(
            "uvloop is not supported on Windows. Use --loop asyncio."
        )

    try:
        uvloop = importlib.import_module("uvloop")
    except ImportError as exc:
        raise CliError(
            "uvloop is not installed. Install it or use --loop asyncio."
        ) from exc

    loop_factory = getattr(uvloop, "new_event_loop", None)
    if loop_factory is None:
        raise CliError(
            "uvloop is installed but does not expose new_event_loop(). Use --loop asyncio."
        )
    return loop, cast(Callable[[], asyncio.AbstractEventLoop], loop_factory)


def _run_cli_awaitable(
    awaitable: Coroutine[Any, Any, object],
    *,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None,
) -> None:
    if loop_factory is None:
        asyncio.run(awaitable)
        return

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(awaitable)


def _is_watch_glob(spec: str) -> bool:
    return any(char in WATCH_GLOB_CHARS for char in spec)


def _normalize_watch_spec(spec: str, *, option_name: str) -> str:
    normalized = spec.strip()
    if not normalized:
        raise CliError(f"{option_name} entries cannot be empty.")
    return normalized


def _coerce_watch_path_spec(spec: str, *, option_name: str) -> _WatchPathSpec:
    normalized = _normalize_watch_spec(spec, option_name=option_name)
    directory_hint = normalized.endswith(("/", "\\"))
    stripped = normalized.rstrip("/\\")
    if not stripped:
        stripped = normalized
    candidate = Path(stripped)
    return _WatchPathSpec(
        path=candidate.resolve(),
        recursive=directory_hint or candidate.is_dir(),
    )


def _path_match_candidates(path: str, *, cwd: Path) -> set[str]:
    resolved_path = Path(path).resolve()
    candidates = {
        Path(path).name,
        path.replace("\\", "/"),
        resolved_path.as_posix(),
    }
    try:
        relative = resolved_path.relative_to(cwd)
    except ValueError:
        return candidates

    relative_text = relative.as_posix()
    candidates.add(relative_text)
    if relative_text != ".":
        candidates.add(f"./{relative_text}")
    return candidates


def _matches_path_spec(path: Path, spec: _WatchPathSpec) -> bool:
    if spec.recursive:
        return path == spec.path or path.is_relative_to(spec.path)
    return path == spec.path


def _nearest_existing_dir(path: Path) -> Path:
    candidate = path if path.is_dir() else path.parent
    while True:
        if candidate.exists():
            return candidate if candidate.is_dir() else candidate.parent
        parent = candidate.parent
        if parent == candidate:
            return Path.cwd().resolve()
        candidate = parent


def _watch_root_for_path_spec(spec: _WatchPathSpec) -> Path:
    return _nearest_existing_dir(spec.path if spec.recursive else spec.path.parent)


def _watch_root_for_glob(spec: str) -> Path:
    normalized = spec.replace("\\", "/")
    is_absolute = normalized.startswith("/")
    prefix_parts: list[str] = []
    for part in normalized.split("/"):
        if not part or part == ".":
            continue
        if _is_watch_glob(part):
            break
        prefix_parts.append(part)

    root = Path("/" if is_absolute else ".")
    if prefix_parts:
        root = root.joinpath(*prefix_parts)
    return _nearest_existing_dir(root.resolve())


def _display_watch_root(path: Path, *, cwd: Path) -> str:
    try:
        relative = path.relative_to(cwd)
    except ValueError:
        return str(path)
    return "." if not relative.parts else relative.as_posix()


def _build_watch_plan(
    *,
    watch_specs: Sequence[str],
    ignore_specs: Sequence[str],
) -> _WatchPlan:
    cwd = Path.cwd().resolve()
    raw_watch_specs = tuple(
        _normalize_watch_spec(spec, option_name="--watch")
        for spec in (watch_specs or (".",))
    )
    raw_ignore_specs = tuple(
        _normalize_watch_spec(spec, option_name="--watch-ignore")
        for spec in (*DEFAULT_WATCH_IGNORE_SPECS, *ignore_specs)
    )

    include_paths = tuple(
        _coerce_watch_path_spec(spec, option_name="--watch")
        for spec in raw_watch_specs
        if not _is_watch_glob(spec)
    )
    include_globs = tuple(spec for spec in raw_watch_specs if _is_watch_glob(spec))
    ignore_paths = tuple(
        _coerce_watch_path_spec(spec, option_name="--watch-ignore")
        for spec in raw_ignore_specs
        if not _is_watch_glob(spec)
    )
    ignore_globs = tuple(spec for spec in raw_ignore_specs if _is_watch_glob(spec))

    roots: list[str] = []
    for path_spec in include_paths:
        roots.append(_display_watch_root(_watch_root_for_path_spec(path_spec), cwd=cwd))
    for glob_spec in include_globs:
        roots.append(_display_watch_root(_watch_root_for_glob(glob_spec), cwd=cwd))

    return _WatchPlan(
        roots=tuple(dict.fromkeys(roots)) or (".",),
        include_paths=include_paths,
        include_globs=include_globs,
        ignore_paths=ignore_paths,
        ignore_globs=ignore_globs,
    )


def _build_watch_filter(plan: _WatchPlan) -> Callable[[Any, str], bool]:
    from watchfiles.filters import DefaultFilter

    cwd = Path.cwd().resolve()
    default_filter = DefaultFilter()

    def _filter(change: Any, path: str) -> bool:
        if not default_filter(change, path):
            return False

        resolved_path = Path(path).resolve()
        candidates = _path_match_candidates(path, cwd=cwd)

        if plan.include_paths or plan.include_globs:
            included_by_path = any(
                _matches_path_spec(resolved_path, spec) for spec in plan.include_paths
            )
            included_by_glob = any(
                fnmatch(candidate, pattern)
                for candidate in candidates
                for pattern in plan.include_globs
            )
            if not (included_by_path or included_by_glob):
                return False

        if any(_matches_path_spec(resolved_path, spec) for spec in plan.ignore_paths):
            return False

        return not any(
            fnmatch(candidate, pattern)
            for candidate in candidates
            for pattern in plan.ignore_globs
        )

    return _filter


def _check_unix_socket_supported(unix_socket: str | None) -> None:
    """``AF_UNIX`` is Unix-only on most Python builds; Windows may lack it entirely."""
    if unix_socket is None:
        return
    if not hasattr(socket, "AF_UNIX"):
        raise CliError(
            "Unix domain sockets are not available on this platform. "
            "Use --host and --port instead."
        )


def _join_command_line_for_watchfiles(argv: list[str]) -> str:
    """Command string for watchfiles; must match ``watchfiles.run.split_cmd`` (POSIX vs Windows)."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def serve_once(
    app_spec: str,
    *,
    loop: CliLoop,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compression: CompressionConfig,
    max_request_header_bytes: int,
    max_request_body_bytes: int,
) -> None:
    """CLI entry: load bootstrap, pick tracer, construct ``Server``, block until shutdown signal."""
    _check_unix_socket_supported(unix_socket)
    loop_name, loop_factory = _resolve_cli_loop(loop)
    bootstrap = load_bootstrap(app_spec)
    tracer = resolve_tracer_factory(tracer_spec)()

    async def runner() -> None:
        with tracer:
            server = Server(
                bootstrap,
                tracer,
                host=host,
                port=port,
                unix_socket=unix_socket,
                compression=compression,
                event_loop_name=loop_name,
                max_request_header_bytes=max_request_header_bytes,
                max_request_body_bytes=max_request_body_bytes,
            )
            await server.run()

    _run_cli_awaitable(runner(), loop_factory=loop_factory)


def _serve_command_argv(
    app_spec: str,
    *,
    loop: CliLoop,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compression: CompressionConfig,
    max_request_header_bytes: int,
    max_request_body_bytes: int,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "stario.cli",
        "serve",
        app_spec,
        "--loop",
        loop,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if unix_socket is not None:
        argv.extend(["--unix-socket", unix_socket])
    argv.extend(
        [
            "--max-request-header-bytes",
            str(max_request_header_bytes),
            "--max-request-body-bytes",
            str(max_request_body_bytes),
        ]
    )
    if tracer_spec not in (None, "", "auto"):
        argv.extend(["--tracer", tracer_spec])
    argv.extend(
        [
            "--compress-min-size",
            str(compression.min_size),
            "--compress-zstd-level",
            str(compression.zstd_level),
        ]
    )
    if compression.zstd_window_log is not None:
        argv.extend(["--compress-zstd-window-log", str(compression.zstd_window_log)])
    argv.extend(
        [
            "--compress-brotli-level",
            str(compression.brotli_level),
        ]
    )
    if compression.brotli_window_log is not None:
        argv.extend(
            ["--compress-brotli-window-log", str(compression.brotli_window_log)]
        )
    argv.extend(
        [
            "--compress-gzip-level",
            str(compression.gzip_level),
        ]
    )
    if compression.gzip_window_bits is not None:
        argv.extend(
            ["--compress-gzip-window-bits", str(compression.gzip_window_bits)]
        )
    return argv


def _build_serve_command(
    app_spec: str,
    *,
    loop: CliLoop,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compression: CompressionConfig,
    max_request_header_bytes: int,
    max_request_body_bytes: int,
) -> str:
    return _join_command_line_for_watchfiles(
        _serve_command_argv(
            app_spec,
            loop=loop,
            tracer_spec=tracer_spec,
            host=host,
            port=port,
            unix_socket=unix_socket,
            compression=compression,
            max_request_header_bytes=max_request_header_bytes,
            max_request_body_bytes=max_request_body_bytes,
        )
    )


def watch_app(
    app_spec: str,
    *,
    loop: CliLoop,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compression: CompressionConfig,
    max_request_header_bytes: int,
    max_request_body_bytes: int,
    watch_specs: Sequence[str],
    watch_ignore_specs: Sequence[str] = (),
) -> None:
    """Restart the dev server when matched paths change (requires ``watchfiles``)."""
    _check_unix_socket_supported(unix_socket)
    try:
        from watchfiles import run_process as watch_run_process
    except ImportError as exc:
        raise CliError(
            "watchfiles is not installed. Use 'stario serve' or install the dependency."
        ) from exc

    watch_plan = _build_watch_plan(
        watch_specs=watch_specs,
        ignore_specs=watch_ignore_specs,
    )
    _watch_cli_line(
        _cli_style(
            f"Watching {', '.join(watch_plan.roots)} for changes...",
            fg="cyan",
        )
    )

    def _announce_reload(_changes: object) -> None:
        _watch_cli_line(_cli_style("Changes detected, reloading...", fg="yellow"))

    command = _build_serve_command(
        app_spec,
        loop=loop,
        tracer_spec=tracer_spec,
        host=host,
        port=port,
        unix_socket=unix_socket,
        compression=compression,
        max_request_header_bytes=max_request_header_bytes,
        max_request_body_bytes=max_request_body_bytes,
    )
    watch_filter = _build_watch_filter(watch_plan)

    watch_run_process(
        *watch_plan.roots,
        target=command,
        target_type="command",
        callback=_announce_reload,
        watch_filter=watch_filter,
    )
