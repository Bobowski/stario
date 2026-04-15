"""Low-level helpers: ``js`` / ``s`` literals and debounce-throttle modifier strings.

Used by attribute and action builders; you rarely import this module directly.
"""

import re
from collections.abc import Iterable
from typing import Any, Literal

from stario.exceptions import StarioError

# Filter types for include/exclude parameters
FilterValue = str | Iterable[str]


type SignalValue = (
    str | int | float | bool | dict[str, SignalValue] | list[SignalValue] | None
)


# TimeValue: int → Ns suffix, float → seconds scaled to ms in ``time_to_string``, str → passed through.
type TimeValue = int | float | str


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
    """Single-quoted JS string literal (for filters, headers, … inside ``js({...})``).

    ```python
    from stario.datastar.format import js, s

    js(filterSignals={"include": s(r"foo.*")})
    # → "{'filterSignals':{'include':'foo.*'}}"  (JS object literal string for options)
    ```
    """
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
        pairs = (f"{s(str(k))}:{_js_expr(v)}" for k, v in value.items())
        return "{" + ",".join(pairs) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_js_expr(v) for v in value) + "]"
    # Fallback: convert to string expression
    return str(value)


def js(__obj: dict[str, Any] | None = None, /, **kwargs: Any) -> str:
    """Build a minified JS object literal for Datastar option blobs. Values are JS expressions; use ``s()`` for string literals.

    ```python
    import stario.datastar as ds

    ds.get("/api", headers={"X-Client": ds.s("stario")})
    # headers value becomes {'X-Client':'stario'} inside the @get options object
    ds.post("/save", include=["draft"], payload={"title": "el.value"})
    ```
    """
    obj = __obj if __obj is not None else kwargs
    pairs = (f"{s(str(k))}:{_js_expr(v)}" for k, v in obj.items())
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
    if not key:
        return "", "kebab"

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

