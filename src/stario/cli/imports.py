"""Import `module:callable` specs for CLI bootstrap, tracers, and similar entry points."""

import importlib
import re
import sys
from pathlib import Path

from stario.cli.errors import CliError

_cached_cwd_on_syspath: Path | None = None


def _cwd_on_syspath(cwd: Path) -> bool:
    for entry in sys.path:
        try:
            candidate = cwd if entry == "" else Path(entry).resolve()
        except OSError:
            continue
        if candidate == cwd:
            return True
    return False


def _hint_for_import_error(exc: ImportError) -> str | None:
    match = re.match(r"cannot import name '([^']+)' from '([^']+)'", str(exc))
    if match is not None:
        name, package = match.groups()
    else:
        name = getattr(exc, "name", None)
        package = None
        if name is None:
            return None
        module_match = re.search(r"from '([^']+)'", str(exc))
        if module_match is not None:
            package = module_match.group(1)
    if package != "stario" or name is None:
        return None
    return (
        f"'{name}' is not exported from the stario package root. "
        "Import from the submodule that defines it (see stario docs)."
    )


def _format_module_import_error(
    module_name: str,
    *,
    label: str,
    spec: str,
    exc: Exception,
) -> str:
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    lines = [
        f"Could not load {label} '{spec}': importing '{module_name}' failed.",
        "",
        f"  {type(root).__name__}: {root}",
    ]
    hint: str | None = None
    if isinstance(root, ModuleNotFoundError):
        missing = root.name
        if missing == module_name:
            hint = (
                "Check that you are in the project directory and that the module exists. "
                "Multi-file apps often use app.main:bootstrap (not main:bootstrap)."
            )
        elif missing is not None:
            hint = (
                f"No module named {missing!r} — "
                f"imported by '{module_name}' or one of its dependencies."
            )
    elif isinstance(root, ImportError):
        hint = _hint_for_import_error(root)
    if hint is not None:
        lines.extend(["", f"Hint: {hint}"])
    return "\n".join(lines)


def load_symbol(spec: str, *, label: str) -> object:
    """Resolve `module:callable` (dotted attributes allowed) from the current project.

    Inserts the current working directory at the front of `sys.path` when it
    is not already present (console-script entry points often omit it).
    """
    module_name, separator, attr_path = spec.partition(":")
    if not separator or not module_name or not attr_path:
        raise CliError(
            f"Invalid {label} '{spec}'. Use 'module:callable' or 'module:pkg.callable'."
        )

    parts = attr_path.split(".")
    if any(not part for part in parts):
        raise CliError(
            f"Invalid {label} '{spec}'. Dotted attribute paths cannot contain empty segments."
        )

    cwd = Path.cwd().resolve()
    global _cached_cwd_on_syspath
    if not (_cached_cwd_on_syspath == cwd and _cwd_on_syspath(cwd)):
        if not _cwd_on_syspath(cwd):
            sys.path.insert(0, str(cwd))
        _cached_cwd_on_syspath = cwd

    try:
        current: object = importlib.import_module(module_name)
    except (ModuleNotFoundError, ImportError) as exc:
        raise CliError(
            _format_module_import_error(
                module_name,
                label=label,
                spec=spec,
                exc=exc,
            )
        ) from exc
    except Exception as exc:
        raise CliError(
            f"Could not load {label} '{spec}': importing '{module_name}' failed: {exc}"
        ) from exc

    qualname = module_name
    for part in parts:
        try:
            current = getattr(current, part)
        except AttributeError as exc:
            raise CliError(
                f"Could not resolve {label} '{spec}': "
                f"'{qualname}' has no attribute '{part}'."
            ) from exc
        qualname = f"{qualname}.{part}"

    return current
