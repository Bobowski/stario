"""Tag objects that build HTML trees."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from inspect import cleandoc
from typing import cast

from stario.exceptions import StarioError

from .attributes import render_nested, render_styles
from .escape import _normalize_attribute_key, escape_attribute_value
from .types import (
    AttributeDict,
    HtmlElement,
    HtmlElementTuple,
    SafeString,
    TagAttributes,
)


def _render_attribute_key(key: object) -> str:
    if type(key) is SafeString:
        return cast(SafeString, key).safe_str

    if type(key) is str:
        return _normalize_attribute_key(cast(str, key))

    raise StarioError(
        f"Invalid attribute name type: {type(key).__name__}",
        context={
            "attribute": str(key),
            "attribute_type": type(key).__name__,
        },
        help_text="HTML attribute names must be strings or SafeString objects.",
        example=cleandoc(
            """
            from stario.html import Div

            Div({"id": "main"})
            """
        ),
    )


def _append_attribute(parts: list[str], key: str, value: object) -> None:
    value_type = type(value)
    append = parts.append

    if value_type is str:
        append(f' {key}="{escape_attribute_value(cast(str, value))}"')
        return

    if value_type is SafeString:
        append(f' {key}="{cast(SafeString, value).safe_str}"')
        return

    if value is None or value is True:
        append(f" {key}")
        return

    if value is False:
        return

    if value_type is int or value_type is float or value_type is Decimal:
        append(f' {key}="{value}"')
        return

    if isinstance(value, Sequence):
        append(f' {key}="{_render_attribute_list(cast(Sequence[object], value), key)}"')
        return

    if key == "style" and value_type is dict:
        append(f' {key}="{render_styles(cast(AttributeDict, value)).safe_str}"')
        return

    if value_type is dict:
        render_nested(key, cast(AttributeDict, value), append)
        return

    raise StarioError(
        f"Invalid value type for attribute '{key}': {type(value).__name__}",
        context={
            "attribute": key,
            "value_type": type(value).__name__,
            "value": str(value)[:100],
        },
        help_text="HTML attributes support: str, int, float, Decimal, bool, None, list, or dict.",
        example=cleandoc(
            """
            from stario.html import Div

            Div({"class": ["btn", "primary"]})
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
            f"Invalid list item type for attribute '{key}': {type(value).__name__}",
            context={
                "attribute": key,
                "item_type": type(value).__name__,
                "item_value": str(value)[:100],
            },
            help_text=(
                "Attribute list items support: str, SafeString, int, float, or Decimal."
            ),
            example=cleandoc(
                """
                from stario.html import Div

                Div({"class": ["btn", "primary"]})
                """
            ),
        )

    return " ".join(tokens)


class Tag:
    """Callable factory for a single HTML or SVG element name.

    Instantiate with the constructor (e.g. ``P = Tag("p")``). Prefer the
    built-in catalogs in ``stario.html.tags`` and ``stario.html.svg`` where they
    exist. The factory caches opening/closing strings and, for tags with no
    arguments, a ready ``SafeString`` (avoid mutating these attributes after init).

    Calling the instance (``Div(...)``, ``P("hi")``, etc.) builds a tree node;
    see ``Tag.__call__``.
    """

    __slots__ = (
        "tag_start",
        "closing_tag",
        "tag_start_no_attrs",
        "rendered",
        "no_children_close",
        "_repr",
    )

    def __init__(self, name: str, self_closing: bool = False) -> None:
        """Create a tag factory.

        ``name`` is the element name on the wire (e.g. ``"div"``, ``"circle"``).
        Set ``self_closing=True`` for void / self-closing elements so calls with
        no children emit ``/>`` (e.g. ``Img``, ``Br``, ``svg.Circle``).
        """
        self._repr = f"Tag(name='{name}', self_closing={self_closing})"
        self.tag_start = "<" + name
        self.tag_start_no_attrs = self.tag_start + ">"
        self.closing_tag = "</" + name + ">"
        self.no_children_close = "/>" if self_closing else ">" + self.closing_tag
        self.rendered = SafeString(self.tag_start + self.no_children_close)

    def __call__(
        self, *children: TagAttributes | HtmlElement | None
    ) -> HtmlElementTuple | SafeString:
        """Build one element from positional arguments.

        Children are processed in order:

        - Each initial mapping (``dict`` or other ``Mapping``) is merged into the
          opening tag as attributes; several mappings in a row merge left-to-right
          (later keys win). ``None`` entries are skipped.
        - The first non-mapping starts child content. After that, mappings must
          not appear (attributes must come first).

        Return value: a ``(open_tag, children, close_tag)`` tuple for elements with
        children, or a ``SafeString`` for empty or self-closing results. With no
        arguments, returns the cached empty / self-closing markup for this tag.
        """
        if not children:
            return self.rendered

        child_elements: list[HtmlElement] = []
        attribute_parts: list[str] | None = None
        saw_child = False

        for child in children:
            if child is None:
                continue

            # Mappings become attributes. Everything else is a child node.
            if type(child) is dict or isinstance(child, Mapping):
                if saw_child:
                    raise StarioError(
                        "HTML attributes must be passed before children",
                        context={"tag": self._repr},
                        help_text=(
                            "Pass one or more attribute mappings first, then child nodes."
                        ),
                        example=cleandoc(
                            """
                            from stario.html import Div, Span

                            Div({"class": "card"}, Span("ok"))
                            """
                        ),
                    )
                if attribute_parts is None:
                    attribute_parts = []

                mapping = cast(Mapping[object, object], child)
                for raw_key, raw_value in mapping.items():
                    key = _render_attribute_key(raw_key)
                    _append_attribute(attribute_parts, key, raw_value)

                continue

            saw_child = True
            child_elements.append(cast(HtmlElement, child))

        # Build the final leaf or branch node.
        attrs_joined = "".join(attribute_parts or [])
        if child_elements:
            return (
                self.tag_start + attrs_joined + ">",
                child_elements,
                self.closing_tag,
            )

        return SafeString(self.tag_start + attrs_joined + self.no_children_close)

    def __repr__(self) -> str:
        return self._repr
