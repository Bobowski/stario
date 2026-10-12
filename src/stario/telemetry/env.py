"""Tracer backend from `STARIO_TRACER` (and `STARIO_TRACERS_*` settings)."""

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from stario._env import env_str
from stario.exceptions import StarioError

from .core import Tracer
from .noop import NoOpTracer

_CUSTOM_TRACER_FACTORY = "make_tracer"


def _load_symbol(spec: str, *, label: str) -> object:
    """Resolve `module:callable` (dotted attributes allowed) from the project.

    Inserts the current working directory at the front of `sys.path` when it
    is not already present (console-script entry points often omit it).
    """
    module_name, separator, attr_path = spec.partition(":")
    if not separator or not module_name or not attr_path:
        raise StarioError(
            f"Invalid {label} '{spec}'. Use 'module:callable' or 'module:pkg.callable'."
        )
    parts = attr_path.split(".")
    if any(not part for part in parts):
        raise StarioError(
            f"Invalid {label} '{spec}'. Dotted attribute paths cannot contain empty segments."
        )

    cwd = Path.cwd().resolve()
    if not any(
        entry == str(cwd) or (entry == "" and Path.cwd() == cwd) for entry in sys.path
    ):
        sys.path.insert(0, str(cwd))

    try:
        current: object = importlib.import_module(module_name)
    except Exception as exc:
        raise StarioError(
            f"Could not load {label} '{spec}': importing '{module_name}' failed: {exc}"
        ) from exc

    qualname = module_name
    for part in parts:
        try:
            current = getattr(current, part)
        except AttributeError as exc:
            raise StarioError(
                f"Could not resolve {label} '{spec}': "
                f"'{qualname}' has no attribute '{part}'."
            ) from exc
        qualname = f"{qualname}.{part}"
    return current


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
                from .tty import TTYTracer

                return TTYTracer()
            from .json import json_tracer_from_env

            return json_tracer_from_env()
        if builtin == "tty":
            from .tty import TTYTracer

            return TTYTracer()
        if builtin == "json":
            from .json import json_tracer_from_env

            return json_tracer_from_env()
        if builtin == "noop":
            return NoOpTracer()
        if builtin == "sqlite":
            from .sqlite import sqlite_tracer_from_env

            return sqlite_tracer_from_env()
    except ValueError as exc:
        raise StarioError(str(exc)) from exc
    return _custom_tracer_from_spec(effective)


def _custom_tracer_from_spec(spec: str) -> Tracer:
    """Load a custom tracer from `module` (`make_tracer`) or `module:callable`."""
    factory_spec = spec if ":" in spec else f"{spec}:{_CUSTOM_TRACER_FACTORY}"

    try:
        loaded = _load_symbol(factory_spec, label="telemetry output")
    except StarioError as exc:
        if ":" not in spec and f"has no attribute '{_CUSTOM_TRACER_FACTORY}'" in str(
            exc
        ):
            raise StarioError(
                f"Telemetry output '{spec}' has no {_CUSTOM_TRACER_FACTORY}(). "
                f"Add def {_CUSTOM_TRACER_FACTORY}() -> Tracer, "
                "or set STARIO_TRACER=module:callable."
            ) from exc
        raise

    if not callable(loaded):
        raise StarioError(f"Telemetry output '{spec}' must be callable.")
    try:
        tracer = cast(Callable[[], Tracer], loaded)()
    except Exception as exc:
        raise StarioError(f"Telemetry output '{spec}' failed: {exc}") from exc
    for name in ("__enter__", "__exit__", "create", "on_end", "stats"):
        if not callable(getattr(tracer, name, None)):
            raise StarioError(
                f"Telemetry output '{spec}' must return a Tracer (missing {name!r})."
            )
    return tracer
