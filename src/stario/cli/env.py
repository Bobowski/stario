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

_CUSTOM_TRACER_FACTORY = "make_tracer"


def unix_socket_from_env() -> str | None:
    """Read `STARIO_UNIX_SOCKET` without validating the full server config."""
    return env_optional_str("STARIO_UNIX_SOCKET")


def tracer_from_env() -> Tracer:
    """Read `STARIO_TRACER` and optional `STARIO_TRACERS_*` settings.

    Built-in values: `auto` (TTY when stdout is a TTY, else JSON), `tty`,
    `json`, `noop`, `sqlite`, a module that exports `make_tracer()`, or
    `module:callable` for a custom factory that returns a `Tracer`. Custom
    factories must implement `create()` and return spans whose finished
    records work with the bundled `on_end()` export path.
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

        return _custom_tracer_from_spec(effective)
    except (StarioError, ValueError) as exc:
        raise CliError(str(exc)) from exc


def _custom_tracer_from_spec(spec: str) -> Tracer:
    """Load a custom tracer from `module` (`make_tracer`) or `module:callable`."""
    factory_spec = spec if ":" in spec else f"{spec}:{_CUSTOM_TRACER_FACTORY}"

    try:
        loaded = load_symbol(factory_spec, label="telemetry output")
    except CliError as exc:
        if ":" not in spec and f"has no attribute '{_CUSTOM_TRACER_FACTORY}'" in str(
            exc
        ):
            raise CliError(
                f"Telemetry output '{spec}' has no {_CUSTOM_TRACER_FACTORY}(). "
                f"Add def {_CUSTOM_TRACER_FACTORY}() -> Tracer, "
                "or set STARIO_TRACER=module:callable."
            ) from exc
        raise

    if not callable(loaded):
        raise CliError(f"Telemetry output '{spec}' must be callable.")
    try:
        tracer = cast(Callable[[], Tracer], loaded)()
    except Exception as exc:
        raise CliError(f"Telemetry output '{spec}' failed: {exc}") from exc
    for name in ("__enter__", "__exit__", "create", "on_end", "stats"):
        if not callable(getattr(tracer, name, None)):
            raise CliError(
                f"Telemetry output '{spec}' must return a Tracer (missing {name!r})."
            )
    return tracer


def server_config_from_env() -> ServerConfig:
    """Read `STARIO_*` listen, limit, compression, and shutdown settings for `Server`."""
    try:
        return _server_config_from_env()
    except (StarioError, ValueError) as exc:
        raise CliError(str(exc)) from exc
