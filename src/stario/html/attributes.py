"""``style=""`` and prefixed nested attributes (``data-*``, ``aria-*``, ...)."""

from collections.abc import Callable, Sequence
from decimal import Decimal
from inspect import cleandoc
from typing import cast

from stario.exceptions import StarioError

from .constants import COMMON_SAFE_CSS_PROPS
from .escape import escape_attribute_key, escape_attribute_value
from .types import AttributeDict, SafeString


def _render_style_key(key: object) -> str:
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

    if key_str in COMMON_SAFE_CSS_PROPS:
        return key_str

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


def _render_style_value(key: str, value: object) -> str:
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


def _render_attribute_list(values: Sequence[object], key: str) -> str:
    tokens: list[str] = []
    append = tokens.append

    for value in values:
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
            f"Invalid list item type for nested attribute '{key}': {type(value).__name__}",
            context={
                "attribute": key,
                "item_type": type(value).__name__,
                "item_value": str(value)[:100],
            },
            help_text=(
                "Nested attribute list items support: str, SafeString, int, float, or Decimal."
            ),
            example=cleandoc(
                """
                from stario.html import Button

                Button({"data": {"user-id": ["123", "456"]}})
                """
            ),
        )

    return " ".join(tokens)


def _render_nested_key(key: object) -> str:
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
                f' {key_prefix}-{escaped_key}="{_render_attribute_list(cast(Sequence[object], value), f"{key_prefix}-{escaped_key}")}"'
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
