"""HTML types and small helpers."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from stario.exceptions import StarioError

from .escape import escape_text


@dataclass(frozen=True, slots=True)
class SafeString:
    """Markup inserted verbatim into rendered HTML (no escaping)."""

    rendered: str

    def __repr__(self) -> str:
        return f"SafeString({self.rendered!r})"


@dataclass(frozen=True, slots=True)
class Attrs:
    """Trusted/pre-rendered opening-tag attributes, including leading spaces."""

    rendered: str

    def __repr__(self) -> str:
        return f"Attrs({self.rendered!r})"


# Attribute values
type AttributeValue = str | SafeString | bool | int | float | None

# Omitted helper values (styles/class conditionals).
type Omitted = None | Literal[False]

# Inline style declarations — str/int/float only (no SafeString; escape always).
type StyleValue = str | int | float
type StyleDeclarations = Mapping[str, StyleValue | Omitted]

# Class tokens are wire names (escaped); conditionals use truthy mapping values.
type ClassInput = str | Omitted | Mapping[str, Any]

# Plain attribute dictionaries map wire keys to scalar values.
# Prefer TagAttributes for tag positional args; AttributeDict is an alias kept
# for older icon modules.
type AttributeDict = Mapping[str, AttributeValue]

# Tag call arguments
type TagAttributes = Mapping[str, AttributeValue] | Attrs

# Render tree: (tag_start, attrs, children, tail)
# - tag_start: "<name" without ">"
# - attrs: None, joined str (no slots), or list[str | AttrSlot] at bake time
# - children: list of child nodes, or None when tail carries the empty close
# - tail: "</name>" with children, or no_children_close ("/>", "></name>") when empty
type HtmlElementTuple = tuple[
    str,
    str | list[Any] | None,
    list[HtmlElement] | None,
    str,
]
type HtmlElement = (
    str | int | float | SafeString | list[HtmlElement] | HtmlElementTuple | None
)


def Comment(
    content: str | SafeString | int | float | None = "",
) -> SafeString:
    """`<!-- ... -->` with textual content escaped so `-->` cannot break out.

    Parsers do not decode entities inside comments, so escaped characters
    (e.g. `&gt;`) read back literally: escaping protects against breakout,
    not byte-for-byte fidelity. Pass `SafeString` when you control the bytes
    and need them verbatim.
    """
    if content is None:
        comment_text = ""
    elif type(content) is bool:
        raise StarioError(
            "Invalid comment content type: bool",
            context={
                "content_type": type(content).__name__,
                "content_value": str(content)[:100],
            },
            help_text="Comment content supports: str, SafeString, int, float, or None. Convert bool to text explicitly if needed.",
            example="render(Comment(str(is_enabled)))",
        )
    elif type(content) is SafeString:
        comment_text = content.rendered
    elif type(content) is str:
        # Escape the body so `-->` stays inert.
        comment_text = escape_text(content)
    elif type(content) is int or type(content) is float:
        comment_text = str(content)
    else:
        raise StarioError(
            f"Invalid comment content type: {type(content).__name__}",
            context={
                "content_type": type(content).__name__,
                "content_value": str(content)[:100],
            },
            help_text="Comment content supports: str, SafeString, int, float, or None.",
            example='render(Comment("Build marker"))',
        )

    return SafeString(f"<!--{comment_text}-->")
