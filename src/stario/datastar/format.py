"""
Datastar formatting utilities.

This module contains small helper types + functions that are shared by
Datastar attribute and action builders.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from typing import Any, Literal

from stario.exceptions import StarioError

# Filter types for include/exclude parameters
FilterValue = str | Iterable[str]


type SignalValue = (
    str | int | float | bool | dict[str, SignalValue] | list[SignalValue] | None
)


type TimeValue = int | float | str
"""
We measure time in seconds.
int = number of whole seconds, adds "s" suffix. 10 => 10s.
float = possible fractional seconds, adds "ms" suffix. 0.5 => 500ms.
str = no parsing
"""


def time_to_string(time: TimeValue) -> str:
    """
    Convert a time value to a Datastar-compatible time string.

    Args:
        time: Time value as int (seconds), float (seconds), or string

    Returns:
        Formatted time string for Datastar attributes
    """
    if isinstance(time, float):
        return f"{int(time * 1000)}ms"
    if isinstance(time, int):
        return f"{int(time)}s"
    return time


type Debounce = (
    TimeValue
    | tuple[TimeValue, Literal["leading", "notrailing"]]
    | tuple[
        TimeValue, Literal["leading", "notrailing"], Literal["leading", "notrailing"]
    ]
)


type Throttle = (
    TimeValue
    | tuple[TimeValue, Literal["noleading", "trailing"]]
    | tuple[
        TimeValue, Literal["noleading", "trailing"], Literal["noleading", "trailing"]
    ]
)


def debounce_to_string(debounce: Debounce) -> str:
    """Convert a debounce configuration to a Datastar modifier string."""
    if isinstance(debounce, (int, float, str)):
        return "debounce." + time_to_string(debounce)

    if len(debounce) == 2:
        return f"debounce.{time_to_string(debounce[0])}.{debounce[1]}"

    if len(debounce) == 3:
        return f"debounce.{time_to_string(debounce[0])}.{debounce[1]}.{debounce[2]}"

    raise StarioError(
        f"Invalid debounce configuration: {debounce}",
        context={
            "debounce_value": str(debounce),
            "debounce_type": type(debounce).__name__,
        },
        help_text="Debounce must be a time value (int/float/str) or a tuple with time and modifiers.",
    )


def throttle_to_string(throttle: Throttle) -> str:
    """Convert a throttle configuration to a Datastar modifier string."""
    if isinstance(throttle, (int, float, str)):
        return "throttle." + time_to_string(throttle)

    if len(throttle) == 2:
        return f"throttle.{time_to_string(throttle[0])}.{throttle[1]}"

    if len(throttle) == 3:
        return f"throttle.{time_to_string(throttle[0])}.{throttle[1]}.{throttle[2]}"

    raise StarioError(
        f"Invalid throttle configuration: {throttle}",
        context={
            "throttle_value": str(throttle),
            "throttle_type": type(throttle).__name__,
        },
        help_text="Throttle must be a time value (int/float/str) or a tuple with time and modifiers.",
    )


def s(value: str) -> str:
    """Wrap a string as a JS string literal (single-quoted). Short for 'string'."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _js_expr(value: Any) -> str:
    """Convert a Python value to a JavaScript expression."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # String values are treated as JS expressions (e.g., signal names)
        return value
    if isinstance(value, dict):
        pairs = (f"'{k}':{_js_expr(v)}" for k, v in value.items())
        return "{" + ",".join(pairs) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_js_expr(v) for v in value) + "]"
    # Fallback: convert to string expression
    return str(value)


def js(__obj: dict[str, Any] | None = None, /, **kwargs: Any) -> str:
    """
    Build a JavaScript object literal. Values are JS expressions.

    Usage:
        js(active="isActive", count=5)           → {'active':isActive,'count':5}
        js({"active": "isActive"})               → {'active':isActive}
        js(include=s("pattern"))                 → {'include':'pattern'}

    Use s() to wrap string literals that should be quoted in JS.
    """
    obj = __obj if __obj is not None else kwargs
    pairs = (f"'{k}':{_js_expr(v)}" for k, v in obj.items())
    return "{" + ",".join(pairs) + "}"


def parse_filter_value(value: FilterValue) -> str:
    """Parse a filter value for include/exclude parameters in Datastar actions."""
    if isinstance(value, str):
        return value
    escaped_items = [re.escape(str(item)) for item in value]
    return "|".join(escaped_items)


type Case = Literal["kebab", "snake", "pascal", "camel"]


def to_kebab_key(key: str) -> tuple[str, Case]:
    """Convert a key to kebab-case and detect its original casing style."""
    if "_" in key:
        return key.replace("_", "-").lower(), "snake"

    if "-" in key:
        return key.lower(), "kebab"

    if key.islower():
        return key, "kebab"

    if key[0].isupper():
        return (
            "".join(
                (
                    ("-" if i != 0 and c.isupper() else "") + c.lower()
                    for i, c in enumerate(key)
                )
            ),
            "pascal",
        )

    return (
        "".join((("-" + c.lower()) if c.isupper() else c for c in key)),
        "camel",
    )


# =============================================================================
# Signal Value Conversion
# =============================================================================

# Type alias for values that can be converted to signal dicts
type SignalData = dict[str, Any] | Any  # Any includes dataclass, Pydantic, TypedDict


def _is_pydantic_model(obj: Any) -> bool:
    """Check if an object is a Pydantic model instance."""
    return hasattr(obj, "model_dump") and hasattr(obj, "model_fields")


def signal_to_dict(data: SignalData) -> dict[str, Any]:
    """
    Convert signal data to a JSON-serializable dict.

    Supports:
    - dict → pass through
    - dataclass → convert via asdict()
    - Pydantic model → convert via model_dump()
    - TypedDict → already a dict at runtime

    Args:
        data: Signal data (dict, dataclass, Pydantic model, or TypedDict)

    Returns:
        Dict ready for JSON serialization

    Raises:
        StarioError: If data cannot be converted to a dict

    Examples:
        @dataclass
        class Signals:
            count: int = 0

        signal_to_dict(Signals(count=5))  # {"count": 5}
        signal_to_dict({"count": 5})      # {"count": 5}
    """
    # Already a dict (includes TypedDict at runtime)
    if isinstance(data, dict):
        return data

    # Pydantic model
    if _is_pydantic_model(data):
        return data.model_dump()

    # Dataclass instance
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        return dataclasses.asdict(data)

    # Cannot convert
    type_name = type(data).__name__
    raise StarioError(
        f"Cannot convert {type_name} to signal dict",
        context={
            "value_type": type_name,
            "value": repr(data)[:100],
        },
        help_text="w.sync() accepts dict, dataclass instance, or Pydantic model.",
        example="""# Using dataclass:
@dataclass
class Signals:
    count: int = 0
    name: str = ""

w.sync(Signals(count=5, name="test"))

# Using dict:
w.sync({"count": 5, "name": "test"})""",
    )
