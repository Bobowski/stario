"""Wire-format helpers for HTML attribute values (shared by Tag, attributes, baked)."""

from typing import cast

from stario.exceptions import StarioError

from .escape import escape_attribute_value
from .types import AttributeValue, SafeString


def wire_scalar_attr_fragment(key: str, value: AttributeValue) -> str | None:
    """Return a wire fragment for one scalar attribute value, or None to omit.

    `key` must already be validated (`validate_attribute_key`).
    """
    value_type = type(value)

    if value_type is str:
        return f' {key}="{escape_attribute_value(cast(str, value))}"'

    if value_type is SafeString:
        return f' {key}="{cast(SafeString, value).rendered}"'

    if value is True:
        return f" {key}"

    if value is None or value is False:
        return None

    if value_type is int or value_type is float:
        return f' {key}="{value}"'

    raise StarioError(
        f"Invalid value type for attribute '{key}': {type(value).__name__}",
        context={
            "attribute": key,
            "value_type": type(value).__name__,
            "value": str(value)[:100],
        },
        help_text=(
            "HTML attributes support scalar values only: str, SafeString, int, "
            "float, bool, or None. Use helpers like classes(), styles(), or data() "
            "for common formatting."
        ),
        example='h.Div({"id": "main"})',
    )
