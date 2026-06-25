"""CLI helpers: import `module:bootstrap`, resolve tracers, serve/watch.

Bootstrap must be an async generator with a single `yield`. In tests use
`async with stario.testing.TestClient(bootstrap)` (pytest-asyncio).

Server runtime policy is read from `STARIO_*` environment variables
(see `stario.cli.env`).
"""

import inspect
import math
import shlex
import socket
import sys
from collections.abc import Sequence
from fnmatch import translate as glob_to_regex
from pathlib import Path
from typing import cast

from stario.cli.env import (
    server_config_from_env,
    tracer_from_env,
    unix_socket_from_env,
)
from stario.cli.errors import CliError
from stario.cli.imports import load_symbol
from stario.cli.term import echo, style
from stario.exceptions import StarioError
from stario.http.bootstrap import Bootstrap
from stario.http.server import Server

WATCH_GLOB_CHARS = "*?["
WATCH_PATH_SEPARATORS = "/\\"
WATCH_IGNORE_ENTITY_SUFFIXES = (
    r"\.sqlite3(?:-.+)?$",
    r"\.sqlite(?:-.+)?$",
    r"\.db(?:-.+)?$",
)


def load_bootstrap(spec: str) -> Bootstrap:
    """Import `module:callable` for `Server`."""
    bootstrap = load_symbol(spec, label="app")
    if not callable(bootstrap):
        raise CliError(f"App '{spec}' must be callable.")
    if not inspect.isasyncgenfunction(bootstrap):
        raise CliError(
            f"App '{spec}' must be an async generator function "
            "(async def bootstrap(app, span): ...; yield)."
        )
    return cast(Bootstrap, bootstrap)


def _check_unix_socket_supported(unix_socket: str | None) -> None:
    """`AF_UNIX` is Unix-only on most Python builds; Windows may lack it entirely."""
    if unix_socket is None:
        return
    if not hasattr(socket, "AF_UNIX"):
        raise CliError(
            "Unix domain sockets are not available on this platform. "
            "Unset STARIO_UNIX_SOCKET or use STARIO_HOST and STARIO_PORT."
        )


def serve_once(app_spec: str) -> None:
    """CLI entry: load bootstrap, pick tracer, construct `Server`, block until shutdown."""
    config = server_config_from_env()
    _check_unix_socket_supported(config.unix_socket)

    bootstrap = load_bootstrap(app_spec)
    tracer = tracer_from_env()
    try:
        with tracer:
            Server(bootstrap, tracer, config=config).run()
    except StarioError as exc:
        raise CliError(str(exc)) from exc


def watch_app(
    app_spec: str,
    *,
    watch_specs: Sequence[str],
    watch_ignore_specs: Sequence[str] = (),
) -> None:
    """Restart the dev server when watched paths change.

    Uses watchfiles `run_process` with a `stario serve` subprocess per reload so
    reload children do not re-import watchfiles through multiprocessing spawn.
    Each reload is a full re-import with a fresh read of `STARIO_*` env vars.
    watchfiles debounces changes by about 1.6s by default. If the child exits
    with an error, the parent keeps watching but does not restart until the next
    file change.

    `--watch` is path-only: a watched file reloads when that file changes; a
    watched directory reloads for any child path under it because watchfiles
    watches recursively by default.

    `--watch-ignore` accepts paths and simple filename globs. An ignored file is
    skipped; an ignored directory skips everything below that directory. A glob
    such as `*.db` matches a file or directory name anywhere under the watched
    paths. Path globs such as `data/*.db` are deliberately not supported.
    """
    from watchfiles import run_process
    from watchfiles.filters import DefaultFilter

    _check_unix_socket_supported(unix_socket_from_env())

    stripped = tuple(spec.strip() for spec in watch_specs)
    paths = tuple(dict.fromkeys(stripped)) or (".",)
    ignore_paths: list[str] = []
    ignore_globs: list[str] = []

    for spec in paths:
        if not spec:
            raise CliError("--watch entries cannot be empty.")
        if any(char in spec for char in WATCH_GLOB_CHARS):
            raise CliError(
                "--watch expects paths, not glob patterns; "
                "watch directories recursively instead."
            )
        if not Path(spec).exists():
            raise CliError(f"--watch path does not exist: {spec!r}")

    for spec in (spec.strip() for spec in watch_ignore_specs):
        if not spec:
            raise CliError("--watch-ignore entries cannot be empty.")
        if any(char in spec for char in WATCH_GLOB_CHARS):
            if any(separator in spec for separator in WATCH_PATH_SEPARATORS):
                raise CliError(
                    "--watch-ignore supports simple filename globs only; "
                    "use a directory path for path ignores."
                )
            ignore_globs.append(spec)
        else:
            if not Path(spec).exists():
                raise CliError(f"--watch-ignore path does not exist: {spec!r}")
            ignore_paths.append(spec)

    watch_filter = DefaultFilter(
        ignore_entity_patterns=(
            *DefaultFilter.ignore_entity_patterns,
            *WATCH_IGNORE_ENTITY_SUFFIXES,
            *(glob_to_regex(pattern) for pattern in ignore_globs),
        ),
        ignore_paths=tuple(Path(spec).resolve() for spec in ignore_paths),
    )
    echo(
        style(
            f"Watching {', '.join(paths)} for changes...",
            fg="cyan",
        )
    )

    def on_reload(_changes: object) -> None:
        echo(style("Changes detected, reloading...", fg="yellow"))

    config = server_config_from_env()
    # Match watchfiles' wait budget to the server's drain window (+ force-close cap).
    sigint_timeout = max(1, math.ceil(config.graceful_shutdown_timeout + 1.0))
    serve_command = " ".join(
        shlex.quote(part)
        for part in (sys.executable, "-m", "stario.cli", "serve", app_spec)
    )

    run_process(
        *paths,
        target=serve_command,
        target_type="command",
        callback=on_reload,
        watch_filter=watch_filter,
        sigint_timeout=sigint_timeout,
    )
