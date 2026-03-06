"""Runtime helpers for `stario serve` and `stario watch`."""

import asyncio
import importlib
import inspect
import shlex
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncContextManager, TypeGuard, cast

import click

from stario.exceptions import StarioError
from stario.http.server import Server, StarioBootstrap
from stario.http.writer import CompressionConfig
from stario.telemetry import JsonTracer, RichTracer, Tracer
from stario.telemetry.core import Span

type TracerFactory = Callable[[], Tracer]
type BootstrapResult = (
    AsyncContextManager[object] | AsyncIterator[None] | Awaitable[object] | None
)
type BootstrapCandidate = Callable[["Stario", Span], BootstrapResult]

if TYPE_CHECKING:
    from stario.http.app import Stario


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
        raise click.ClickException(
            f"Invalid {label} '{spec}'. Use 'module:callable'."
        )

    _ensure_cwd_on_syspath()

    try:
        current: object = importlib.import_module(module_name)
    except Exception as exc:
        raise click.ClickException(
            f"Could not import module '{module_name}' for {label} '{spec}': {exc}"
        ) from exc

    for part in attr_path.split("."):
        try:
            current = getattr(current, part)
        except AttributeError as exc:
            raise click.ClickException(
                f"Module '{module_name}' does not define '{attr_path}' for {label} '{spec}'."
            ) from exc

    return current


def _is_async_context_manager(
    value: object,
) -> TypeGuard[AsyncContextManager[object]]:
    return hasattr(value, "__aenter__") and hasattr(value, "__aexit__")


@asynccontextmanager
async def _bootstrap_async_generator_scope(
    generator: AsyncIterator[None],
) -> AsyncIterator[None]:
    try:
        await anext(generator)
    except StopAsyncIteration as exc:
        raise StarioError(
            "Bootstrap async generator did not yield",
            help_text="Add a single `yield` for teardown support or return normally for setup-only bootstraps.",
        ) from exc

    try:
        yield
    finally:
        try:
            await anext(generator)
        except StopAsyncIteration:
            pass
        else:
            raise StarioError(
                "Bootstrap async generator yielded more than once",
                help_text="Bootstrap async generators must yield exactly once.",
            )


def normalize_bootstrap(bootstrap: BootstrapCandidate) -> StarioBootstrap:
    @asynccontextmanager
    async def wrapped(app: "Stario", span: Span) -> AsyncIterator[None]:
        result = bootstrap(app, span)

        if _is_async_context_manager(result):
            async with result:
                yield
            return

        if inspect.isasyncgen(result):
            async with _bootstrap_async_generator_scope(result):
                yield
            return

        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)
            yield
            return

        if result is None:
            yield
            return

        raise StarioError(
            "Unsupported bootstrap return type",
            context={"type": type(result).__name__},
            help_text="Return an async context manager, an async generator, an awaitable, or None.",
        )

    return wrapped


def load_bootstrap(spec: str) -> StarioBootstrap:
    bootstrap = _load_symbol(spec, label="app")
    if not callable(bootstrap):
        raise click.ClickException(f"App '{spec}' must be callable.")
    return normalize_bootstrap(cast(BootstrapCandidate, bootstrap))


def default_tracer_factory() -> TracerFactory:
    return RichTracer if sys.stdout.isatty() else JsonTracer


def resolve_tracer_factory(spec: str | None) -> TracerFactory:
    if spec in (None, "", "auto"):
        return default_tracer_factory()
    if spec == "rich":
        return RichTracer
    if spec == "json":
        return JsonTracer

    factory = _load_symbol(spec, label="telemetry output")
    if not callable(factory):
        raise click.ClickException(f"Telemetry output '{spec}' must be callable.")
    return cast(TracerFactory, factory)


def serve_once(
    app_spec: str,
    *,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compression: CompressionConfig,
) -> None:
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
            )
            await server.run()

    asyncio.run(runner())


def _build_serve_command(
    app_spec: str,
    *,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compression: CompressionConfig,
) -> str:
    command = [
        sys.executable,
        "-m",
        "stario.cli",
        "serve",
        app_spec,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if unix_socket is not None:
        command.extend(["--unix-socket", unix_socket])
    if tracer_spec not in (None, "", "auto"):
        command.extend(["--tracer", tracer_spec])
    command.extend(
        [
            "--compress-min-size",
            str(compression.min_size),
            "--compress-zstd-level",
            str(compression.zstd_level),
            "--compress-brotli-level",
            str(compression.brotli_level),
            "--compress-gzip-level",
            str(compression.gzip_level),
        ]
    )
    return shlex.join(command)


def watch_app(
    app_spec: str,
    *,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compression: CompressionConfig,
    watch_paths: Sequence[str],
) -> None:
    try:
        from watchfiles import run_process as watch_run_process
    except ImportError as exc:
        raise click.ClickException(
            "watchfiles is not installed. Use 'stario serve' or install the dependency."
        ) from exc

    resolved_watch_paths = [str(Path(path)) for path in (watch_paths or (".",))]
    click.echo(
        click.style(
            f"Watching {', '.join(resolved_watch_paths)} for changes...",
            fg="cyan",
        )
    )

    def _announce_reload(_changes: object) -> None:
        click.echo(click.style("Changes detected, reloading...", fg="yellow"))

    command = _build_serve_command(
        app_spec,
        tracer_spec=tracer_spec,
        host=host,
        port=port,
        unix_socket=unix_socket,
        compression=compression,
    )

    watch_run_process(
        *resolved_watch_paths,
        target=command,
        target_type="command",
        callback=_announce_reload,
    )
