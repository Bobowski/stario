"""Low-level helpers for JS literals, object expressions, and modifier strings.

Used by attribute and action builders; you rarely import this module directly.
"""

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal, cast

from stario.exceptions import StarioError

_SIGNAL_NAME_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_SIGNAL_PATH_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*)*$"
)
_TIME_VALUE_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:ms|s)$")

# Filter types for include/exclude parameters
FilterValue = str | Iterable[str]

type SignalValue = (
    str | int | float | bool | dict[str, SignalValue] | list[SignalValue] | None
)

# TimeValue: int -> Ns suffix, float -> seconds scaled to ms, str -> explicit `ms`/`s`.
type TimeValue = int | float | str


def require_mapping(name: str, payload: object) -> Mapping[str, Any]:
    """Reject non-mapping signal payloads with a stable TypeError."""
    if not isinstance(payload, Mapping):
        raise TypeError(f"{name} payload must be a mapping")
    return cast(Mapping[str, Any], payload)


def time_to_string(time: TimeValue | None) -> str:
    """Convert a time value to a Datastar-compatible time string.

    `int` values are whole seconds (`1` -> `"1s"`). `float` values are seconds
    converted to milliseconds (`1.0` -> `"1000ms"`). Strings must already be
    explicit whole millisecond or second values, such as `"150ms"` or `"2s"`.
    """
    if time is None:
        raise StarioError(
            f"Invalid Datastar time value: {time!r}",
            context={"time_value": repr(time), "time_type": type(time).__name__},
            help_text="Pass seconds as int/float, or a string like '150ms' or '2s'.",
        )
    if isinstance(time, bool):
        raise StarioError(
            f"Invalid Datastar time value: {time!r}",
            context={"time_value": repr(time), "time_type": type(time).__name__},
            help_text="Pass seconds as int/float, or a string like '150ms' or '2s'.",
        )
    if isinstance(time, float):
        if not math.isfinite(time) or time < 0:
            raise StarioError(
                f"Invalid Datastar time value: {time!r}",
                context={"time_value": repr(time), "time_type": type(time).__name__},
                help_text="Pass a finite non-negative number of seconds.",
            )
        return f"{int(time * 1000)}ms"
    if isinstance(time, int):
        if time < 0:
            raise StarioError(
                f"Invalid Datastar time value: {time!r}",
                context={"time_value": repr(time), "time_type": type(time).__name__},
                help_text="Pass a non-negative number of seconds.",
            )
        return f"{int(time)}s"
    if not _TIME_VALUE_RE.fullmatch(time):
        raise StarioError(
            f"Invalid Datastar time value: {time!r}",
            context={"time_value": time, "time_type": type(time).__name__},
            help_text=(
                "Use whole milliseconds or seconds, for example '150ms' or '2s'. "
                "For fractional seconds, pass a float such as 0.5."
            ),
        )
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

    if len(debounce) not in (2, 3):
        raise StarioError(
            f"Invalid debounce configuration: {debounce}",
            context={
                "debounce_value": str(debounce),
                "debounce_type": type(debounce).__name__,
            },
            help_text="Debounce must be a time value or a tuple with time and modifiers.",
        )

    modifiers = debounce[1:]
    for modifier in modifiers:
        if modifier not in ("leading", "notrailing"):
            raise StarioError(
                f"Invalid debounce modifier: {modifier!r}",
                context={"debounce_modifier": str(modifier)},
                help_text="Debounce modifiers are 'leading' and 'notrailing'.",
            )

    return "debounce." + time_to_string(debounce[0]) + "." + ".".join(modifiers)


def throttle_to_string(throttle: Throttle) -> str:
    """Convert a throttle configuration to a Datastar modifier string."""
    if isinstance(throttle, (int, float, str)):
        return "throttle." + time_to_string(throttle)

    if len(throttle) not in (2, 3):
        raise StarioError(
            f"Invalid throttle configuration: {throttle}",
            context={
                "throttle_value": str(throttle),
                "throttle_type": type(throttle).__name__,
            },
            help_text="Throttle must be a time value or a tuple with time and modifiers.",
        )

    modifiers = throttle[1:]
    for modifier in modifiers:
        if modifier not in ("noleading", "trailing"):
            raise StarioError(
                f"Invalid throttle modifier: {modifier!r}",
                context={"throttle_modifier": str(modifier)},
                help_text="Throttle modifiers are 'noleading' and 'trailing'.",
            )

    return "throttle." + time_to_string(throttle[0]) + "." + ".".join(modifiers)


def string_literal(value: str) -> str:
    """Single-quoted JS string literal for Datastar action/object expressions.

    ```python
    from stario.datastar.format import js_object, string_literal

    js_object(filterSignals={"include": string_literal(r"foo.*")})
    # → "{'filterSignals':{'include':'foo.*'}}"  (JS object literal string for options)
    ```
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"'{escaped}'"


def _js_expr(value: Any) -> str:
    """Convert a Python value to a JavaScript expression."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StarioError(
                f"Cannot render non-finite float as JS expression: {value}",
                context={"value": str(value)},
                help_text="Pass a finite number, or pre-render a JavaScript expression string.",
            )
        return str(value)
    if isinstance(value, str):
        # String values are treated as JS expressions (e.g., signal names)
        return value
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        pair_parts: list[str] = []
        for key, item in mapping.items():
            pair_parts.append(f"{string_literal(str(key))}:{_js_expr(item)}")
        return "{" + ",".join(pair_parts) + "}"
    if isinstance(value, (list, tuple)):
        seq = cast(list[Any] | tuple[Any, ...], value)
        return "[" + ",".join(_js_expr(item) for item in seq) + "]"
    raise StarioError(
        f"Cannot render {type(value).__name__} as JS expression",
        context={"type": type(value).__name__, "value": repr(value)[:100]},
        help_text=(
            "Pass JSON-shaped values, a JavaScript expression string, or wrap text "
            "with string_literal()."
        ),
    )


def js_object(__obj: dict[str, Any] | None = None, /, **kwargs: Any) -> str:
    """Build a minified JavaScript object literal.

    String values are emitted as raw JavaScript expressions, not quoted text.
    Wrap text with `string_literal()` when you want a JavaScript string value.

    ```python
    from stario.datastar import at, format

    at.get("/api", headers={"X-Client": format.string_literal("stario")})
    # headers value becomes {'X-Client':'stario'} inside the @get options object
    at.post("/save", include=["draft"], payload={"title": "el.value"})
    format.js_object({"label": format.string_literal("Save")})
    ```
    """
    obj = __obj if __obj is not None else kwargs
    pairs = (f"{string_literal(str(k))}:{_js_expr(v)}" for k, v in obj.items())
    return "{" + ",".join(pairs) + "}"


def parse_filter_value(value: FilterValue) -> str:
    """Parse a filter value for include/exclude parameters in Datastar actions.

    A string is passed through as a Datastar regex. An iterable is treated as
    literal signal names joined with `|`, with regex metacharacters escaped.
    """
    if isinstance(value, str):
        return value
    return "|".join(re.escape(str(item)) for item in value)


def filter_js(
    include: FilterValue | None,
    exclude: FilterValue | None,
) -> str | None:
    """Build the `{'include':…,'exclude':…}` JS object used by signal filters.

    Returns `None` when neither filter is set, so callers can omit the option entirely.

    ```python
    filter_js(["draft", "attachments"], None)
    # → "{'include':'draft|attachments'}"
    ```
    """
    if include is None and exclude is None:
        return None
    if include is not None:
        include_js = "'include':" + string_literal(parse_filter_value(include))
        if exclude is None:
            return "{" + include_js + "}"
        exclude_js = "'exclude':" + string_literal(parse_filter_value(exclude))
        return "{" + include_js + "," + exclude_js + "}"

    assert exclude is not None
    return "{'exclude':" + string_literal(parse_filter_value(exclude)) + "}"


type Case = Literal["kebab", "snake", "pascal", "camel"]


def validate_signal_name(name: str) -> None:
    """Validate Stario's signal-name contract: Python-facing names are snake_case."""
    if _SIGNAL_NAME_SEGMENT_RE.fullmatch(name):
        return

    raise StarioError(
        f"Invalid Datastar signal name: {name!r}",
        context={"signal_name": name},
        help_text=(
            "Use snake_case signal names in Python, for example `user_id` or "
            "`room_title`. Stario renders the Datastar `__case.snake` modifier "
            "where the wire format needs it."
        ),
    )


def validate_signal_path(path: str) -> None:
    """Validate a snake_case Datastar signal name or dotted signal path."""
    if _SIGNAL_PATH_RE.fullmatch(path):
        return

    raise StarioError(
        f"Invalid Datastar signal path: {path!r}",
        context={"signal_path": path},
        help_text=(
            "Use snake_case names in Python, including every segment of dotted "
            "signal paths, for example `user_id` or `crane.selected_crane`."
        ),
    )


def to_kebab_key(key: str) -> str:
    """Convert a Python/JS-ish key spelling to a Datastar attribute key suffix."""
    if not key:
        return ""

    if "_" in key:
        return key.replace("_", "-").lower()

    if "-" in key:
        return key.lower()

    if key.islower():
        return key

    if key[0].isupper():
        parts: list[str] = []
        for i, c in enumerate(key):
            if c.isupper():
                if i:
                    parts.append("-")
                parts.append(c.lower())
            else:
                parts.append(c)
        return "".join(parts)

    parts = []
    for c in key:
        if c.isupper():
            parts.append("-")
            parts.append(c.lower())
        else:
            parts.append(c)
    return "".join(parts)


def case_modifier(case: Case) -> str:
    """Return the explicit Datastar case modifier, omitting the camel default."""
    return "" if case == "camel" else "__case." + case


def signal_key(name: str) -> str:
    """Return a Datastar namespaced-attribute key suffix for a snake_case signal."""
    validate_signal_name(name)
    return to_kebab_key(name) + "__case.snake"


def signal_path_key(path: str) -> str:
    """Return a namespaced-attribute key suffix for a snake_case signal path."""
    validate_signal_path(path)
    return ".".join(to_kebab_key(part) for part in path.split(".")) + "__case.snake"
