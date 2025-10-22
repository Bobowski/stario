# This module is inspired by simple-html library
# https://github.com/keithasaurus/simple_html
#
# For Stario I needed to add some opinionated decisions for structures
#  that could be used across the framework.
#

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Callable, Iterable

from stario.exceptions import (
    HtmlRenderError,
    InvalidAttributeValueError,
    InvalidStyleValueError,
)

from .safestring import SafeString
from .types import (
    COMMON_SAFE_ATTRIBUTE_NAMES,
    COMMON_SAFE_CSS_PROPS,
    AttributeDict,
    TagAttributes,
)


def faster_escape(s: str) -> str:
    """
    Escape HTML special characters for safe text content.

    This is an optimized version of html.escape() that's faster because:
    - No conditional checks for desired replacements
    - No variable reassignments
    - Direct chaining of replace operations

    Args:
        s: String to escape

    Returns:
        HTML-escaped string safe for text content

    Examples:
        >>> faster_escape("Hello <world>")
        'Hello &lt;world&gt;'
        >>> faster_escape('Say "hello" & \\'goodbye\\'')
        'Say &quot;hello&quot; &amp; &#x27;goodbye&#x27;'
        >>> faster_escape("No special chars")
        'No special chars'
    """
    return (
        s.replace("&", "&amp;")  # Must be done first!
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def escape_attribute_key(k: str) -> str:
    """
    Escape HTML attribute names for safe attribute usage.

    Escapes special characters that could be dangerous in attribute names,
    including HTML entities and additional characters specific to attributes.

    Args:
        k: Attribute name to escape

    Returns:
        HTML-escaped attribute name safe for use in attributes

    Examples:
        >>> escape_attribute_key("data-value")
        'data-value'
        >>> escape_attribute_key("onclick=alert()")
        'onclick&#x3D;alert()'
        >>> escape_attribute_key("class name")
        'class&nbsp;name'
        >>> escape_attribute_key("data-`test`")
        'data-&#x60;test&#x60;'
    """
    return (
        faster_escape(k)
        .replace("=", "&#x3D;")
        .replace("\\", "&#x5C;")
        .replace("`", "&#x60;")
        .replace(" ", "&nbsp;")
    )


type HtmlElement = (
    str
    | int
    | float
    | Decimal
    | SafeString
    | Tag
    | list[HtmlElement]
    | HtmlElementTuple
)
type HtmlElementTuple = tuple[str, list[HtmlElement], str]


class Tag:
    """
    Represents an HTML tag that can be called to create HTML elements.

    A Tag instance represents a specific HTML tag (like 'div', 'p', 'span')
    and can be called with attributes and children to generate HTML elements.
    Supports both regular and self-closing tags.

    Examples:
        >>> div = Tag("div")
        >>> div("Hello")
        ('<div>', ['Hello'], '</div>')
        >>> br = Tag("br", self_closing=True)
        >>> br()
        SafeString('<br/>')
        >>> div({"class": "test"}, "Content")
        ('<div class="test">', ['Content'], '</div>')
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
        """
        Initialize a new HTML tag.

        Args:
            name: HTML tag name (e.g., 'div', 'p', 'span')
            self_closing: Whether this is a self-closing tag (e.g., 'br', 'img')
        """
        self._repr = f"Tag(name='{name}', self_closing={self_closing})"
        self.tag_start = "<" + name
        self.tag_start_no_attrs = self.tag_start + ">"
        self.closing_tag = "</" + name + ">"
        self.no_children_close = "/>" if self_closing else ">" + self.closing_tag
        self.rendered = self.tag_start + self.no_children_close

    def __call__(
        self, *children: TagAttributes | HtmlElement | None
    ) -> HtmlElementTuple | SafeString:
        """
        Create an HTML element with attributes and children.

        Processes attributes and children to generate HTML.
        All dictionaries / mappings are treated as attributes.
        All other values are treated as elements children.

        Args:
            *children: Attributes (dict) and/or child elements

        Returns:
            SafeString for simple elements, or tuple for elements with children

        Examples:
            >>> div = Tag("div")
            >>> p = Tag("p")
            >>> div("Hello")
            ('<div>', ['Hello'], '</div>')
            >>> div({"class": "test"}, "Content")
            ('<div class="test">', ['Content'], '</div>')
            >>> div({"id": "main"}, [p("Item 1"), p("Item 2")])
            ('<div id="main">', [[('<p>', ['Item 1'], '</p>'), ('<p>', ['Item 2'], '</p>')]], '</div>')
            >>> div({"disabled": True, "hidden": False})
            SafeString('<div disabled></div>')
        """
        if not children:
            # no attributes, no children eg <br /> or <div></div>
            return SafeString(self.rendered)

        # Prepare common list for all attributes
        attrs: list[str] = []
        append_attribute = attrs.append

        child_elements: list[HtmlElement] = []
        append_child = child_elements.append

        for child in children:

            if child is None:
                continue

            # Non-dict is assumed to be a child HTML element
            if not isinstance(child, Mapping):
                append_child(child)
                continue

            # Processing attributes from the dict
            for key in child:
                # seems to be faster than using .items()
                val = child[key]

                # optimization: a large portion of attribute keys should be
                # covered by this check. It allows us to skip escaping
                # where it is not needed. Note this is for attribute names only;
                # attributes values are always escaped (when they are `str`s)
                # key_: str
                if key not in COMMON_SAFE_ATTRIBUTE_NAMES:
                    _key = (
                        key.safe_str
                        if isinstance(key, SafeString)
                        else escape_attribute_key(key)
                    )
                elif isinstance(key, SafeString):
                    _key = key.safe_str
                else:
                    _key = key

                # elif TYPE_CHECKING:
                #     assert isinstance(key, str)

                if type(val) is str:
                    append_attribute(f' {_key}="{faster_escape(val)}"')
                    continue

                # if type(val) is SafeString:
                if type(val) is SafeString:
                    append_attribute(f' {_key}="{val.safe_str}"')
                    continue

                if val is None or val is True:
                    # If None or True we add the attribute without a value
                    append_attribute(f" {_key}")
                    continue

                if val is False:
                    # If False then we simply skip the attribute
                    continue

                if isinstance(val, (int, float, Decimal)):
                    append_attribute(f' {_key}="{val}"')
                    continue

                if isinstance(val, list):
                    joined = " ".join(str(v) for v in val)
                    append_attribute(f' {_key}="{faster_escape(joined)}"')
                    continue

                if key == "style" and isinstance(val, dict):
                    # styles = cast(StyleDict, val)
                    append_attribute(f' {_key}="{render_styles(val).safe_str}"')
                    continue

                if isinstance(val, dict):
                    render_nested(_key, val, append_attribute)
                    continue

                raise InvalidAttributeValueError(
                    f"Invalid value type for attribute '{key}': {type(val).__name__}",
                    context={
                        "attribute": key,
                        "value_type": type(val).__name__,
                        "value": str(val)[:100],
                    },
                    help_text="HTML attributes support: str, int, float, Decimal, bool, None, list, or dict (for nested attributes).",
                    example="""from stario.html import div

# Supported attribute value types:
div({"class": "container"})              # str
div({"tabindex": 0})                     # int
div({"opacity": 0.5})                    # float
div({"disabled": True})                  # bool (renders as 'disabled')
div({"hidden": False})                   # bool (attribute omitted)
div({"class": ["btn", "primary"]})       # list (joined with spaces)
div({"data": {"user-id": "123"}})        # dict (nested attributes)
div({"style": {"color": "red"}})         # dict for styles""",
                )

        # If there are children, we return a tuple with the tag start, children, and closing tag
        if child_elements:
            return (
                self.tag_start + "".join(attrs) + ">",
                child_elements,
                self.closing_tag,
            )

        # No children, so we can return the rendered leaf node directly
        return SafeString(self.tag_start + "".join(attrs) + self.no_children_close)

    def __repr__(self) -> str:
        """
        Return string representation of the tag.

        Returns:
            String showing tag name and whether it's self-closing

        Examples:
            >>> Tag("div").__repr__()
            "Tag(name='div', self_closing=False)"
            >>> Tag("br", self_closing=True).__repr__()
            "Tag(name='br', self_closing=True)"
        """
        return self._repr


def _render(nodes: Iterable[HtmlElement], append: Callable[[str], None]) -> None:
    """
    Internal function that efficiently renders HTML elements by mutating
    a list instead of constantly creating new strings. Handles all
    supported HTML element types recursively.
    """
    for node in nodes:
        if type(node) is tuple:
            if len(node) == 3:
                append(node[0])
                _render(node[1], append)
                append(node[2])
                continue

            else:
                raise HtmlRenderError(
                    f"Invalid tuple length for HTML element: {len(node)}",
                    context={
                        "node_type": type(node).__name__,
                        "node_value": str(node)[:100],
                    },
                    help_text="HTML elements must be tuples with three elements: start tag, children, and end tag.",
                    example="""from stario.html import div, p, render

# Correct rendering:
html = render(div({"class": "container"}, p("Hello")))
""",
                )

        if type(node) is SafeString:
            append(node.safe_str)
            continue

        if type(node) is str:
            append(faster_escape(node))
            continue

        if isinstance(node, list):
            _render(node, append)
            continue

        if type(node) is Tag:
            append(node.rendered)
            continue

        if isinstance(node, (int, float, Decimal)):
            append(str(node))
            continue

        raise HtmlRenderError(
            f"Cannot render element of type {type(node).__name__}",
            context={
                "node_type": type(node).__name__,
                "node_value": str(node)[:100],
            },
            help_text="Only str, int, float, Decimal, SafeString, Tag, list, or tuple elements can be rendered.",
            example="""from stario.html import div, p, SafeString

# Supported element types:
div("text")                    # str
div(42)                        # int, float, Decimal
div(SafeString("<b>bold</b>"))  # SafeString (not escaped)
div(p("paragraph"))            # Tag
div([p("one"), p("two")])      # list of elements

# Incorrect - custom objects need to be converted:
# div(my_object)  # ❌ Won't work
div(str(my_object))  # ✅ Convert to string first""",
        )


def render_styles(styles: AttributeDict) -> SafeString:
    """
    Render a dictionary of CSS styles into a CSS string.

    Converts a dictionary of CSS property-value pairs into a properly
    escaped CSS string suitable for use in style attributes.

    Args:
        styles: Dictionary mapping CSS properties to values

    Returns:
        SafeString containing the CSS string

    Examples:
        >>> render_styles({"color": "red", "font-size": "16px"})
        SafeString('color:red;font-size:16px;')
        >>> render_styles({"background-color": "#fff", "margin": "10px"})
        SafeString('background-color:#fff;margin:10px;')
        >>> render_styles({"opacity": 0.5, "z-index": 100})
        SafeString('opacity:0.5;z-index:100;')
    """
    ret: list[str] = []
    append = ret.append

    for key in styles:

        value = styles[key]

        # Escape key
        if key not in COMMON_SAFE_CSS_PROPS:
            if type(key) is str:
                key = faster_escape(key)
            elif type(key) is SafeString:
                key = key.safe_str
            else:
                raise InvalidStyleValueError(
                    f"Invalid CSS property name type: {type(key).__name__}",
                    context={
                        "property": str(key),
                        "property_type": type(key).__name__,
                    },
                    help_text="CSS property names must be strings or SafeString objects.",
                    example="""from stario.html import div

# Correct style property names:
div({"style": {"color": "red"}})                # str keys
div({"style": {"font-size": "16px"}})           # str with hyphens
div({"style": {"background-color": "#fff"}})    # str

# Incorrect:
# div({"style": {123: "value"}})  # ❌ Numbers not allowed""",
                )

        elif TYPE_CHECKING:
            assert isinstance(key, str)

        # Escape value
        if type(value) is str:
            value = faster_escape(value)
        elif type(value) is SafeString:
            value = value.safe_str
        # note that ints and floats pass through these condition checks

        append(f"{key}:{value};")

    return SafeString("".join(ret))


def render(*nodes: HtmlElement) -> str:
    """
    Render HTML elements into a string.

    Converts HTML elements (tags, text, etc.) into a complete HTML string.
    Handles nested elements, attributes, and various data types.


    Args:
        *nodes: HTML elements to render (tags, text, lists, etc.)

    Returns:
        Complete HTML string

    Raises:
        HtmlRenderError: If rendering fails or unknown element types are encountered

    Examples:
        >>> from stario.html import div, p
        >>> render(div("Hello"), p("World"))
        '<div>Hello</div><p>World</p>'
        >>> render("Plain text", div({"class": "test"}, "Content"))
        'Plain text<div class="test">Content</div>'
        >>> render(div([p("Item 1"), p("Item 2")]))
        '<div><p>Item 1</p><p>Item 2</p></div>'
    """
    try:
        results: list[str] = []
        _render(nodes, results.append)
        return "".join(results)

    except HtmlRenderError:
        # Re-raise our own exceptions without wrapping
        raise
    except Exception as e:
        raise HtmlRenderError(
            f"Unexpected error while rendering HTML: {type(e).__name__}: {e}",
            context={
                "error_type": type(e).__name__,
                "error_message": str(e),
                "node_count": len(nodes) if hasattr(nodes, "__len__") else "unknown",
            },
            help_text="Check that all HTML elements are valid types and properly structured.",
            example="""from stario.html import div, p, render

# Correct rendering:
html = render(div({"class": "container"}, p("Hello")))

# Make sure all elements are proper types:
# ❌ render(my_custom_object)
# ✅ render(str(my_custom_object))
# ✅ render(div(str(my_custom_object)))""",
        ) from e


def render_nested(
    key_prefix: str, data: AttributeDict, append: Callable[[str], None]
) -> None:
    """
    Render nested attributes with a key prefix.

    Used for data-* attributes and other nested attribute structures.
    Each key in the data dictionary becomes a prefixed attribute.

    Args:
        key_prefix: Prefix for all attribute keys (e.g., "data")
        data: Dictionary of nested attributes
        append: Function to append rendered attributes

    Examples:
        >>> attrs = []
        >>> render_nested("data", {"user": "john", "id": 123}, attrs.append)
        >>> attrs
        [' data-user="john"', " data-id='123'"]

        >>> attrs = []
        >>> render_nested("aria", {"label": "Close", "hidden": True}, attrs.append)
        >>> attrs
        [' aria-label="Close"', ' aria-hidden']
    """
    for key in data:
        value = data[key]

        # Escape key
        if type(key) is SafeString:
            key = key.safe_str
        elif TYPE_CHECKING:
            assert isinstance(key, str)
        else:
            key = faster_escape(key)

        # Escape value
        if type(value) is str:
            append(f' {key_prefix}-{key}="{faster_escape(value)}"')
            continue

        if type(value) is SafeString:
            append(f' {key_prefix}-{key}="{value.safe_str}"')
            continue

        if value is None or value is True:
            append(f" {key_prefix}-{key}")
            continue

        if value is False:
            continue

        if isinstance(value, (int, float, Decimal)):
            append(f" {key_prefix}-{key}='{str(value)}'")
            continue

        if type(value) is list:
            joined = " ".join(str(v) for v in value)
            append(f" {key_prefix}-{key}='{faster_escape(joined)}'")
            continue

        raise InvalidAttributeValueError(
            f"Invalid value type for nested attribute '{key_prefix}-{key}': {type(value).__name__}",
            context={
                "attribute_prefix": key_prefix,
                "attribute_name": key,
                "value_type": type(value).__name__,
                "value": str(value)[:100],
            },
            help_text="Nested attributes (like data-* or aria-*) support: str, int, float, Decimal, bool, None, or list.",
            example="""from stario.html import button

# Correct nested attributes (data-* example):
button({"data": {"user-id": "123"}})           # str
button({"data": {"count": 42}})                # int
button({"data": {"enabled": True}})            # bool
button({"data": {"tags": ["tag1", "tag2"]}})   # list

# Renders as:
# <button data-user-id="123" data-count="42" data-enabled data-tags="tag1 tag2">

# Same works for aria-*, hx-*, etc.""",
        )
