"""Tag objects that build HTML trees."""

import re
from collections.abc import Mapping
from typing import Literal, cast

from stario.exceptions import StarioError

from .escape import validate_attribute_key
from .slots import AttrSlot, bake_slot_if_present, slot_name
from .types import (
    AttributeValue,
    Attrs,
    HtmlElement,
    HtmlElementTuple,
    SafeString,
    TagAttributes,
)
from .wire import wire_scalar_attr_fragment

type _AttributePart = str | AttrSlot
type EmptyMode = Literal["normal", "void", "self_closing_when_empty"]

_TAG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_EMPTY_MODES = frozenset({"normal", "void", "self_closing_when_empty"})


def _attrs_before_children_error(tag_repr: str) -> StarioError:
    return StarioError(
        "HTML attributes must be passed before children",
        context={"tag": tag_repr},
        help_text=(
            "Pass attribute mappings and Attrs fragments first, then child nodes."
        ),
        example='h.Div(classes("card"), h.Span("ok"))',
    )


class Tag:
    """Callable factory for a single HTML or SVG element name.

    Instantiate with the constructor (e.g. `P = Tag("p")`). Prefer the
    built-in catalogs in `stario.markup.html` and `stario.markup.svg` where they
    exist. The factory caches opening/closing strings and, for tags with no
    arguments, a ready `SafeString` (avoid mutating these attributes after init).

    `empty="normal"` — no children renders `<tag></tag>`; children allowed.
    `empty="void"` — no children renders `<tag/>`; children are an error
    (HTML `br`, `img`, `input`, …).
    `empty="self_closing_when_empty"` — no children renders `<tag/>`; children
    allowed when present (SVG leaves like `circle`, `path`, filter primitives).

    Calling the instance (`h.Div(...)`, `h.P("hi")`, etc.) builds a tree node;
    see `Tag.__call__`.
    """

    __slots__ = (
        "_empty",
        "closing_tag",
        "empty",
        "name",
        "no_children_close",
        "tag_start",
        "tag_start_no_attrs",
    )

    def __init__(
        self,
        name: str,
        *,
        empty: EmptyMode = "normal",
        prefix: str = "",
    ) -> None:
        """Create a tag factory.

        `name` is the element name on the wire (e.g. `"div"`, `"circle"`).
        Use `empty="void"` for HTML void elements (children are an error), or
        `empty="self_closing_when_empty"` for SVG-style leaves that self-close
        only when called with no children. Optional `prefix` is prepended to the
        opening tag (e.g. `<!doctype html>` on `HtmlDocument`).
        """
        if not _TAG_NAME_RE.fullmatch(name):
            raise StarioError(
                f"Invalid tag name: {name!r}",
                context={"tag": name[:100]},
                help_text=(
                    "Tag names must start with an ASCII letter and then contain only "
                    "ASCII letters, digits, or hyphens."
                ),
                example='CustomElement = Tag("my-widget")',
            )
        if empty not in _EMPTY_MODES:
            raise StarioError(
                f"Invalid empty mode for <{name}>: {empty!r}",
                context={"tag": name, "empty": str(empty)},
                help_text=(
                    'Use empty="normal", empty="void", or '
                    'empty="self_closing_when_empty".'
                ),
            )

        self.name = name
        self._empty = empty
        self.tag_start = prefix + "<" + name
        self.tag_start_no_attrs = self.tag_start + ">"
        self.closing_tag = f"</{name}>"
        self.no_children_close = "/>" if empty != "normal" else ">" + self.closing_tag
        self.empty = SafeString(self.tag_start + self.no_children_close)

    def _repr(self) -> str:
        return f"Tag(name='{self.name}', empty='{self._empty}')"

    def __call__(
        self, *children: TagAttributes | HtmlElement | None
    ) -> HtmlElementTuple | SafeString:
        """Build one element from positional arguments.

        Children are processed in order:

        - Each initial mapping (`dict` or other `Mapping`) is rendered as flat
          `key=value` attributes in the order it appears. Several mappings in a
          row are allowed, but they are not merged or deduplicated; duplicate
          attributes render as duplicate attributes. `None` entries are skipped.
        - The first non-mapping starts child content. After that, mappings must
          not appear (attributes must come first).

        Return value: a `(open, attrs, children, tail)` four-tuple, or a cached
        `SafeString` when there are no arguments. With no attributes, `open` is
        the tag start including `>` (`tag_start_no_attrs`); otherwise `open` is
        `"<name"` and `attrs` is a joined string or slot list. `children` is
        `None` for empty elements; `tail` is `no_children_close` or `closing_tag`.
        """
        # Attribute rendering is a dynamic boundary: public call types stay typed,
        # while raw mapping contents are validated here with exact type checks.
        if not children:
            return self.empty

        child_elements: list[HtmlElement] | None = None
        attribute_parts: list[_AttributePart] | None = None
        has_slots = False
        tag_repr = self._repr()

        for child in children:
            if child is None:
                continue

            child_type = type(child)

            # `Attrs` fragments are pre-rendered attribute strings.
            if child_type is Attrs:
                # `type(child) is Attrs` is intentional; cast tells pyright the branch.
                attrs_fragment = cast(Attrs, child)
                if child_elements is not None:
                    raise _attrs_before_children_error(tag_repr)
                if attribute_parts is None:
                    attribute_parts = [attrs_fragment.rendered]
                else:
                    attribute_parts.append(attrs_fragment.rendered)
                continue

            # {key: value} attribute mappings are merged into the opening tag.
            if child_type is dict or isinstance(child, Mapping):
                if child_elements is not None:
                    raise _attrs_before_children_error(tag_repr)
                if attribute_parts is None:
                    attribute_parts = []

                attr_mapping = cast(Mapping[str, object], child)
                for raw_key, raw_value in attr_mapping.items():
                    bake_key_slot = bake_slot_if_present(raw_key)
                    if bake_key_slot is not None:
                        raise StarioError(
                            "@baked: parameters cannot be used as attribute names",
                            context={"tag": tag_repr},
                            help_text=(
                                "Parameters may hold attribute values or children, "
                                "never attribute names."
                            ),
                        )

                    key = validate_attribute_key(raw_key)

                    bake_value_slot = bake_slot_if_present(raw_value)
                    if bake_value_slot is not None:
                        attribute_parts.append(
                            AttrSlot(key, slot_name(bake_value_slot))
                        )
                        has_slots = True
                        continue

                    fragment = wire_scalar_attr_fragment(
                        key, cast(AttributeValue, raw_value)
                    )
                    if fragment is not None:
                        attribute_parts.append(fragment)

                continue

            # Child content starts here (Attrs and mappings handled above).
            if child_elements is None:
                child_elements = []
            child_elements.append(cast(HtmlElement, child))

        # Void elements cannot have children.
        if self._empty == "void" and child_elements is not None:
            raise StarioError(
                f"<{self.name}> is a void element and cannot have children",
                context={"tag": tag_repr},
                help_text=(
                    "Void elements (br, img, input, ...) always render as a single "
                    "self-closing tag. Put the content in a sibling or parent element."
                ),
                example='h.Div(h.Img({"src": "x.png", "alt": "X"}), "caption")',
            )

        # Build the final leaf or branch node (always a 4-tuple except cached empty).
        if not attribute_parts and child_elements is None:
            return self.empty

        if not attribute_parts:
            # No attributes: <name> or <name>/>.
            open_tag = self.tag_start_no_attrs
            attrs = None
        elif has_slots:
            # Bake-time only: @baked slots in attributes.
            open_tag = self.tag_start
            attrs = attribute_parts
        else:
            # Normal attributes: <name attr1="value1" attr2="value2">.
            # has_slots is False here, so parts are plain str fragments only.
            open_tag = self.tag_start
            attrs = "".join(cast(list[str], attribute_parts))

        if child_elements is None:
            # Empty element: <name/> or <name></name>.
            return (open_tag, attrs, None, self.no_children_close)

        # Element with children: <name attr1="value1" attr2="value2">...</name>.
        return (open_tag, attrs, child_elements, self.closing_tag)

    def __repr__(self) -> str:
        return self._repr()
