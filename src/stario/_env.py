"""Internal environment parsing helpers for env-backed framework configuration."""

import os
from pathlib import Path


def _raw(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def env_str(name: str, default: str) -> str:
    return _raw(name) or default


def env_optional_str(name: str, default: str | None = None) -> str | None:
    raw = _raw(name)
    if raw is None:
        return default
    return raw


def env_int(name: str, default: int) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (got {raw!r}).") from exc


def env_optional_int(name: str, default: int | None = None) -> int | None:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (got {raw!r}).") from exc


def env_float(name: str, default: float) -> float:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number (got {raw!r}).") from exc


def env_bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    text = raw.lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean (got {raw!r}).")


def env_octal_mode(name: str, default: int) -> int:
    """Parse a Unix file mode in octal (`660` → `0o660`; `0o`-prefixed also accepted)."""
    raw = _raw(name)
    if raw is None:
        return default
    text = raw.lower()
    try:
        value = int(text, 0) if text.startswith("0o") else int(text, 8)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a Unix file mode in octal (got {raw!r})."
        ) from exc
    if not 0 <= value <= 0o7777:
        raise ValueError(f"{name} must be between 0 and 7777 (octal).")
    return value


def env_path(name: str, default: str) -> Path:
    """Unset → default; set but empty/whitespace → error (unlike other helpers)."""
    if name not in os.environ:
        return Path(default)
    raw = os.environ[name].strip()
    if not raw:
        raise ValueError(f"{name} must not be empty")
    return Path(raw)
