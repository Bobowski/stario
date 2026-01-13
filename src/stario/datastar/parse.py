"""
Signal Parsing - Convert raw dicts to typed structures.

Supports three schema types (auto-detected):
1. dataclasses - Standard library, recommended
2. TypedDict - For simple type hints without validation
3. Pydantic models - If pydantic is installed, uses model_validate

Type coercion is automatic for basic types:
- str → int, float (parsed)
- str → bool ("true"/"false"/"1"/"0")
- Any value → str (converted)

Class analysis is cached (lru_cache) so repeated parsing is fast.
"""

from dataclasses import MISSING, dataclass, fields, is_dataclass
from functools import lru_cache
from typing import (
    Any,
    NotRequired,
    Required,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from stario.exceptions import StarioError

# =============================================================================
# Type Coercion
# =============================================================================


def _coerce(value: Any, expected_type: type) -> Any:
    """
    Coerce a value to the expected type.

    Handles common web form scenarios:
    - String "42" → int 42
    - String "3.14" → float 3.14
    - String "true"/"false" → bool True/False
    - String "1"/"0" → bool True/False
    - Optional[X] → coerce to X if not None
    - list[X] → coerce each item to X

    Args:
        value: The raw value (usually from JSON/query string)
        expected_type: The target Python type

    Returns:
        Value coerced to expected_type, or original value if coercion fails
    """
    if value is None:
        return None

    origin = get_origin(expected_type)

    # Handle Union types (e.g., Optional[int] = int | None)
    # Try each non-None type in order
    if origin is type(None):
        return None

    # Python 3.10+ union types: int | None, str | int, etc.
    import types

    if origin is types.UnionType or (
        origin is not None and getattr(origin, "__name__", None) == "Union"
    ):
        # Get the union args (e.g., (int, None) for Optional[int])
        from typing import get_args

        args = get_args(expected_type)

        # Filter out None type and try coercion with each remaining type
        non_none_types = [t for t in args if t is not type(None)]
        if not non_none_types:
            return value

        # Try coercing to each type in order
        for candidate_type in non_none_types:
            try:
                result = _coerce(value, candidate_type)
                # If coercion succeeded (value changed type or matched)
                if result is not value or isinstance(value, candidate_type):
                    return result
            except (ValueError, TypeError):
                continue

        # Fallback to first non-None type
        return _coerce(value, non_none_types[0])

    # Handle list[X] - coerce each element
    if origin is list:
        from typing import get_args

        args = get_args(expected_type)
        if args and isinstance(value, list):
            item_type = args[0]
            return [_coerce(item, item_type) for item in value]
        return value

    # Handle dict[K, V] - pass through
    if origin is dict:
        return value

    # Handle other generics (set, tuple, etc.) - pass through
    if origin is not None:
        return value

    # Boolean needs special handling for JS strings
    if expected_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    # Numeric types
    if expected_type is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        try:
            return int(value)
        except (ValueError, TypeError):
            return value

    if expected_type is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        try:
            return float(value)
        except (ValueError, TypeError):
            return value

    # String
    if expected_type is str:
        return str(value) if value is not None else ""

    # Unknown type - return as-is
    return value


# =============================================================================
# Schema Analysis (Cached)
# =============================================================================


@dataclass(frozen=True, slots=True)
class FieldInfo:
    """Cached information about a field in a schema."""

    name: str
    type: type
    required: bool
    default: Any


@lru_cache(maxsize=128)
def _analyze_dataclass(cls: type) -> tuple[FieldInfo, ...]:
    """
    Analyze a dataclass and cache field information.

    Uses lru_cache to avoid repeated introspection on the same class.
    The cache key is the class itself (by identity).

    Args:
        cls: A dataclass type

    Returns:
        Tuple of FieldInfo for each field (frozen for hashability)
    """
    hints = get_type_hints(cls)
    result = []

    for field in fields(cls):
        has_default = (
            field.default is not MISSING  # Has actual default
            or field.default_factory is not MISSING  # Has factory
        )
        result.append(
            FieldInfo(
                name=field.name,
                type=hints.get(field.name, Any),
                required=not has_default,
                default=field.default if field.default is not MISSING else None,
            )
        )

    return tuple(result)


@lru_cache(maxsize=128)
def _analyze_typeddict(cls: type) -> tuple[FieldInfo, ...]:
    """
    Analyze a TypedDict and cache field information.

    TypedDict fields can be:
    - Required (default) - must be present
    - NotRequired - optional
    - Or the class can set total=False (all optional)

    Args:
        cls: A TypedDict type

    Returns:
        Tuple of FieldInfo for each field
    """
    hints = get_type_hints(cls)

    # Get required/optional keys
    required_keys = getattr(cls, "__required_keys__", frozenset())
    # optional_keys = getattr(cls, "__optional_keys__", frozenset())

    result = []
    for name, field_type in hints.items():
        # Check if field is wrapped in Required[] or NotRequired[]
        origin = get_origin(field_type)
        if origin is Required:
            required = True
        elif origin is NotRequired:
            required = False
        else:
            # Use class-level required/optional sets
            required = name in required_keys

        result.append(
            FieldInfo(
                name=name,
                type=field_type,
                required=required,
                default=None,
            )
        )

    return tuple(result)


# =============================================================================
# Parsing Functions
# =============================================================================


def _parse_dataclass[T](data: dict[str, Any], cls: type[T]) -> T:
    """
    Parse a dict into a dataclass instance.

    Process:
    1. Get cached field info for the class
    2. For each field, extract value from data (or use default)
    3. Coerce value to expected type
    4. Construct dataclass with kwargs

    Args:
        data: Raw dict (from JSON/query string)
        cls: Target dataclass type

    Returns:
        Instance of cls with values from data
    """
    field_info = _analyze_dataclass(cls)
    kwargs = {}

    for field in field_info:
        if field.name in data:
            kwargs[field.name] = _coerce(data[field.name], field.type)
        # Missing fields: dataclass will use its defaults

    return cls(**kwargs)


def _parse_typeddict[T](data: dict[str, Any], cls: type[T]) -> T:
    """
    Parse a dict into a TypedDict.

    Note: TypedDict is just a dict at runtime, but we still
    coerce values to their annotated types for consistency.

    Process:
    1. Get cached field info for the TypedDict
    2. For each field present in data, coerce to expected type
    3. Return as dict (TypedDict is just a type hint)

    Args:
        data: Raw dict (from JSON/query string)
        cls: Target TypedDict type

    Returns:
        Dict with coerced values (typed as cls for IDE support)
    """
    field_info = _analyze_typeddict(cls)
    result: dict[str, Any] = {}

    for field in field_info:
        if field.name in data:
            result[field.name] = _coerce(data[field.name], field.type)
        elif field.required:
            # Required field missing - could raise, but we'll be lenient
            pass

    return result  # type: ignore


def _parse_pydantic[T](data: dict[str, Any], cls: type[T]) -> T:
    """
    Parse a dict using Pydantic's model_validate.

    This is a lazy import - only called if we detect a Pydantic model.
    Pydantic handles all validation and coercion internally.

    Args:
        data: Raw dict
        cls: Pydantic BaseModel subclass

    Returns:
        Validated Pydantic model instance
    """
    # model_validate is the Pydantic v2 method
    return cls.model_validate(data)  # type: ignore


def _is_pydantic_model(cls: type) -> bool:
    """
    Check if a class is a Pydantic BaseModel (without importing pydantic).

    We check for the presence of model_validate method which is
    the Pydantic v2 signature. This avoids importing pydantic
    if it's not being used.
    """
    return hasattr(cls, "model_validate") and hasattr(cls, "model_fields")


# =============================================================================
# Main Entry Point
# =============================================================================


def parse_signals[T](data: dict[str, Any], schema: type[T]) -> T:
    """
    Parse raw signal data into a typed structure.

    Auto-detects schema type and uses appropriate parser:
    - Pydantic model → uses model_validate (full validation)
    - dataclass → uses field introspection + coercion
    - TypedDict → uses type hints + coercion

    Type coercion is applied for basic types (int, float, bool, str).
    Complex types are passed through as-is.

    Examples:
        @dataclass
        class FormData:
            count: int = 0
            name: str = ""

        result = parse_signals({"count": "42"}, FormData)
        # FormData(count=42, name="")

        class MySignals(TypedDict):
            active: bool
            tags: list[str]

        result = parse_signals({"active": "true"}, MySignals)
        # {"active": True}

    Args:
        data: Raw dict from request (JSON body or query params)
        schema: Target type (dataclass, TypedDict, or Pydantic model)

    Returns:
        Instance of schema with values parsed from data

    Raises:
        TypeError: If schema is not a supported type
    """
    # Check Pydantic first (most specific)
    if _is_pydantic_model(schema):
        return _parse_pydantic(data, schema)

    # Check dataclass
    if is_dataclass(schema):
        return _parse_dataclass(data, schema)

    # Check TypedDict
    if is_typeddict(schema):
        return _parse_typeddict(data, schema)

    raise StarioError(
        f"Unsupported schema type for signals parsing: {schema.__name__ if hasattr(schema, '__name__') else schema}",
        context={"schema": str(schema), "schema_type": type(schema).__name__},
        help_text="Use a dataclass, TypedDict, or Pydantic BaseModel to parse signals.",
        example="""@dataclass
class MySignals:
    count: int = 0
    name: str = ""

signals = await c.signals(MySignals)""",
    )
