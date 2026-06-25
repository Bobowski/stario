"""Server runtime configuration from `STARIO_*` environment variables.

Use this module in the CLI (`stario serve`, `stario watch`). Library code that
constructs `Server` directly should call `stario.http.config.server_config_from_env`
instead — it raises `StarioError`, not `CliError`.
"""

import sys
from collections.abc import Callable
from typing import cast

from stario._env import env_optional_str, env_str
from stario.cli.errors import CliError
from stario.cli.imports import load_symbol
from stario.exceptions import StarioError
from stario.http.config import ServerConfig
from stario.http.config import server_config_from_env as _server_config_from_env
from stario.telemetry.core import Tracer
from stario.telemetry.json import json_tracer_from_env
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.sqlite import sqlite_tracer_from_env
from stario.telemetry.tty import TTYTracer


def unix_socket_from_env() -> str | None:
    """Read `STARIO_UNIX_SOCKET` without validating the full server config."""
    return env_optional_str("STARIO_UNIX_SOCKET")


def tracer_from_env() -> Tracer:
    """Read `STARIO_TRACER` and optional `STARIO_TRACERS_*` settings.

    Built-in values: `auto` (TTY when stdout is a TTY, else JSON), `tty`,
    `json`, `noop`, `sqlite`, or `module:callable` for a custom factory that
    returns a `Tracer`. Custom factories must implement `create()` and return
    spans whose finished records work with the bundled `on_end()` export path.
    """
    effective = env_str("STARIO_TRACER", "auto")
    builtin = effective.lower()
    try:
        if builtin == "auto":
            if sys.stdout.isatty():
                return TTYTracer()
            return json_tracer_from_env()
        if builtin == "tty":
            return TTYTracer()
        if builtin == "json":
            return json_tracer_from_env()
        if builtin == "noop":
            return NoOpTracer()
        if builtin == "sqlite":
            return sqlite_tracer_from_env()

        loaded = load_symbol(effective, label="telemetry output")
        if not callable(loaded):
            raise CliError(f"Telemetry output '{effective}' must be callable.")
        try:
            tracer = cast(Callable[[], Tracer], loaded)()
        except Exception as exc:
            raise CliError(f"Telemetry output '{effective}' failed: {exc}") from exc
        for name in ("__enter__", "__exit__", "create", "on_end", "stats"):
            if not callable(getattr(tracer, name, None)):
                raise CliError(
                    f"Telemetry output '{effective}' must return a Tracer (missing {name!r})."
                )
        return tracer
    except (StarioError, ValueError) as exc:
        raise CliError(str(exc)) from exc


def server_config_from_env() -> ServerConfig:
    """Read `STARIO_*` listen, limit, compression, and shutdown settings for `Server`."""
    try:
        return _server_config_from_env()
    except (StarioError, ValueError) as exc:
        raise CliError(str(exc)) from exc
