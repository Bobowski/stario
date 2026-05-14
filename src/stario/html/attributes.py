"""``style=""`` and prefixed nested attributes (``data-*``, ``aria-*``, ...)."""

from collections.abc import Callable, Sequence
from decimal import Decimal
from functools import lru_cache
from inspect import cleandoc
from typing import Any, cast

from stario.exceptions import StarioError

from .escape import escape_attribute_key, escape_attribute_value
from .types import AttributeDict, SafeString


@lru_cache(maxsize=512)
def _normalize_style_property_key(key_str: str) -> str:
    """Validate a style property name and return the wire key (escaped via shared LRU)."""
    if key_str.startswith("@"):
        raise StarioError(
            f"Inline style attributes do not support at-rules like '{key_str}'",
            context={"property": key_str},
            help_text="Use normal CSS property names inside style dictionaries, not @rules.",
            example=cleandoc(
                """
                from stario.html import Div

                Div({"style": {"color": "red"}})
                """
            ),
        )

    if any(ch in key_str for ch in ":;{}"):
        raise StarioError(
            f"Invalid CSS property name: {key_str!r}",
            context={"property": key_str},
            help_text=(
                "CSS property names must not contain ':', ';', '{', or '}' characters."
            ),
            example=cleandoc(
                """
                from stario.html import Div

                Div({"style": {"background-color": "red"}})
                """
            ),
        )

    return escape_attribute_value(key_str)


def _render_style_key(key: Any) -> str:
    if type(key) is str:
        key_str = cast(str, key)
    elif type(key) is SafeString:
        key_str = cast(SafeString, key).safe_str
    else:
        raise StarioError(
            f"Invalid CSS property name type: {type(key).__name__}",
            context={
                "property": str(key),
                "property_type": type(key).__name__,
            },
            help_text="CSS property names must be strings or SafeString objects.",
            example=cleandoc(
                """
                from stario.html import Div

                Div({"style": {"color": "red"}})
                """
            ),
        )

    return _normalize_style_property_key(key_str)


def _render_style_value(key: str, value: Any) -> str:
    value_type = type(value)

    if value_type is str:
        return escape_attribute_value(cast(str, value))

    if value_type is SafeString:
        return cast(SafeString, value).safe_str

    if value_type is int or value_type is float or value_type is Decimal:
        return str(value)

    raise StarioError(
        f"Invalid CSS value type for property '{key}': {type(value).__name__}",
        context={
            "property": key,
            "value_type": type(value).__name__,
            "value": str(value)[:100],
        },
        help_text="CSS values must be str, SafeString, int, float, or Decimal.",
        example=cleandoc(
            """
            from stario.html import Div

            Div({"style": {"color": "red"}})
            """
        ),
    )


def _join_attribute_token_list(
    values: Sequence[Any], key: str, *, nested: bool = False
) -> str:
    """Join space-separated attribute tokens (e.g. ``class``, list-valued ``data-*``).

    ``None`` and ``False`` omit a slot (conditional tokens). ``True`` is invalid:
    token lists are not HTML boolean attributes; use a string or rely on
    short-circuit expressions that yield ``False`` when off
    (``active and \"active\"``).
    """
    tokens: list[str] = []
    append = tokens.append
    label = "nested attribute" if nested else "attribute"

    for value in values:
        if value is None or value is False:
            continue

        if value is True:
            raise StarioError(
                f"Invalid list item for {label} '{key}': bool True is not a token",
                context={
                    "attribute": key,
                    "item_type": "bool",
                    "item_value": "True",
                },
                help_text=(
                    "List-valued attributes accept token strings (and numbers). "
                    "Omit a slot with None or False — e.g. "
                    "`name if condition else False`. "
                    "Do not pass bare True."
                ),
                example=cleandoc(
                    """
                    from stario.html import Div

                    Div({"class": ["btn", "primary", is_active and "active"]})
                    """
                ),
            )

        value_type = type(value)

        if value_type is str:
            append(escape_attribute_value(cast(str, value)))
            continue

        if value_type is SafeString:
            append(cast(SafeString, value).safe_str)
            continue

        if value_type is int or value_type is float or value_type is Decimal:
            append(str(value))
            continue

        raise StarioError(
            f"Invalid list item type for {label} '{key}': {type(value).__name__}",
            context={
                "attribute": key,
                "item_type": type(value).__name__,
                "item_value": str(value)[:100],
            },
            help_text=(
                "List items may be str, SafeString, int, float, or Decimal; "
                "None and False skip a slot."
            ),
            example=cleandoc(
                """
                from stario.html import Div, Button

                Div({"class": ["btn", "primary"]})
                Button({"data": {"user-id": ["123", "456"]}})
                """
            ),
        )

    return " ".join(tokens)


def _render_nested_key(key: Any) -> str:
    if type(key) is str:
        return escape_attribute_key(cast(str, key))

    if type(key) is SafeString:
        return cast(SafeString, key).safe_str

    raise StarioError(
        f"Invalid nested attribute name type: {type(key).__name__}",
        context={
            "attribute_name": str(key),
            "attribute_name_type": type(key).__name__,
        },
        help_text="Nested attribute names must be strings or SafeString objects.",
        example=cleandoc(
            """
            from stario.html import Button

            Button({"data": {"user-id": "123"}})
            """
        ),
    )


def render_styles(styles: AttributeDict) -> SafeString:
    """Join CSS declarations for a ``style`` attribute value (already escaped)."""
    ret: list[str] = []
    append = ret.append

    for key in styles:
        value = styles[key]
        rendered_key = _render_style_key(key)
        append(f"{rendered_key}:{_render_style_value(rendered_key, value)};")

    return SafeString("".join(ret))


def render_nested(
    key_prefix: str, data: AttributeDict, append: Callable[[str], None]
) -> None:
    """Emit ``prefix-name="value"`` fragments; used for ``data``, ``aria``, ``hx``, etc."""
    for key in data:
        value = data[key]
        escaped_key = _render_nested_key(key)

        if type(value) is str:
            append(
                f' {key_prefix}-{escaped_key}="{escape_attribute_value(cast(str, value))}"'
            )
            continue

        if type(value) is SafeString:
            append(f' {key_prefix}-{escaped_key}="{cast(SafeString, value).safe_str}"')
            continue

        if value is None or value is True:
            append(f" {key_prefix}-{escaped_key}")
            continue

        if value is False:
            continue

        if isinstance(value, (int, float, Decimal)):
            append(f' {key_prefix}-{escaped_key}="{str(value)}"')
            continue

        if isinstance(value, Sequence):
            append(
                f' {key_prefix}-{escaped_key}="{_join_attribute_token_list(cast(Sequence[Any], value), f"{key_prefix}-{escaped_key}", nested=True)}"'
            )
            continue

        raise StarioError(
            f"Invalid value type for nested attribute '{key_prefix}-{escaped_key}': {type(value).__name__}",
            context={
                "attribute_prefix": key_prefix,
                "attribute_name": escaped_key,
                "value_type": type(value).__name__,
                "value": str(value)[:100],
            },
            help_text="Nested attributes support: str, int, float, Decimal, bool, None, or list.",
            example=cleandoc(
                """
                from stario.html import Button

                Button({"data": {"user-id": "123"}})
                """
            ),
        )
