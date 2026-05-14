"""Tag objects that build HTML trees."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from inspect import cleandoc
from typing import Any

from stario.exceptions import StarioError

from .attributes import _join_attribute_token_list, render_nested, render_styles
from .escape import escape_attribute_key, escape_attribute_value
from .types import (
    HtmlElement,
    HtmlElementTuple,
    SafeString,
    TagAttributes,
)


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
        self.closing_tag = f"</{name}>"
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
        # Attribute rendering is a dynamic boundary: public call types stay typed,
        # while raw mapping contents are validated here with exact type checks.
        if not children:
            return self.rendered

        child_elements: list[HtmlElement] | None = None
        attribute_parts: list[str] | None = None

        for child in children:
            if child is None:
                continue

            # Mappings become attributes. Everything else is a child node.
            if type(child) is dict or isinstance(child, Mapping):
                raw_attrs: Mapping[Any, Any] = child
                if child_elements is not None:
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

                for raw_key, raw_value in raw_attrs.items():
                    # Attribute key normalization.
                    raw_key_type = type(raw_key)
                    if raw_key_type is str:
                        key = escape_attribute_key(raw_key)
                    elif raw_key_type is SafeString:
                        key = raw_key.safe_str
                    else:
                        raise StarioError(
                            f"Invalid attribute name type: {type(raw_key).__name__}",
                            context={
                                "attribute": str(raw_key),
                                "attribute_type": type(raw_key).__name__,
                            },
                            help_text="HTML attribute names must be strings or SafeString objects.",
                            example=cleandoc(
                                """
                                from stario.html import Div

                                Div({"id": "main"})
                                """
                            ),
                        )

                    # Attribute value rendering.
                    value_type = type(raw_value)
                    append = attribute_parts.append

                    if value_type is str:
                        append(f' {key}="{escape_attribute_value(raw_value)}"')
                        continue

                    if value_type is SafeString:
                        append(f' {key}="{raw_value.safe_str}"')
                        continue

                    if value_type is dict:
                        if key == "style":
                            append(f' {key}="{render_styles(raw_value).safe_str}"')
                        else:
                            render_nested(key, raw_value, append)
                        continue

                    if raw_value is None or raw_value is True:
                        append(" " + key)
                        continue

                    if raw_value is False:
                        continue

                    if (
                        value_type is int
                        or value_type is float
                        or value_type is Decimal
                    ):
                        append(f' {key}="{raw_value}"')
                        continue

                    if isinstance(raw_value, Sequence):
                        append(
                            f' {key}="{_join_attribute_token_list(raw_value, key)}"'
                        )
                        continue

                    raise StarioError(
                        f"Invalid value type for attribute '{key}': {type(raw_value).__name__}",
                        context={
                            "attribute": key,
                            "value_type": type(raw_value).__name__,
                            "value": str(raw_value)[:100],
                        },
                        help_text="HTML attributes support: str, int, float, Decimal, bool, None, list, or dict.",
                        example=cleandoc(
                            """
                            from stario.html import Div

                            Div({"class": ["btn", "primary"]})
                            """
                        ),
                    )

                continue

            # Child collection is lazy so attribute-only calls avoid allocating a list.
            if child_elements is None:
                child_elements = []
            child_elements.append(child)

        # Build the final leaf or branch node.
        if not attribute_parts:
            if child_elements is not None:
                return (
                    self.tag_start_no_attrs,
                    child_elements,
                    self.closing_tag,
                )

            return self.rendered

        attrs_joined = "".join(attribute_parts)
        if child_elements is not None:
            return (
                f"{self.tag_start}{attrs_joined}>",
                child_elements,
                self.closing_tag,
            )

        return SafeString(f"{self.tag_start}{attrs_joined}{self.no_children_close}")

    def __repr__(self) -> str:
        return self._repr
