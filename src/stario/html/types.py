"""HTML types and small helpers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from inspect import cleandoc

from stario.exceptions import StarioError

from .escape import escape_text


@dataclass(frozen=True, slots=True)
class SafeString:
    """Markup inserted verbatim into rendered HTML (no escaping)."""

    safe_str: str

    def __repr__(self) -> str:
        return f"SafeString({self.safe_str!r})"


# Attribute values
type AttributeValue = (
    str | SafeString | bool | int | float | Decimal | None | Sequence[AttributeValue]
)

# Nested mappings keep plain ``dict[str, ...]`` assignable.
type AttributeDict = Mapping[str, AttributeValue]

# Tag call arguments
type TagAttributes = Mapping[str, AttributeValue | AttributeDict]


# Render tree
type HtmlElementTuple = tuple[str, list[HtmlElement], str]
type HtmlElement = (
    str
    | int
    | float
    | Decimal
    | SafeString
    | list[HtmlElement]
    | HtmlElementTuple
)


def Comment(
    content: str | SafeString | int | float | Decimal | None = "",
) -> SafeString:
    """``<!-- ... -->`` with textual content escaped so ``-->`` cannot break out."""
    if content is None:
        comment_text = ""
    elif type(content) is bool:
        raise StarioError(
            "Invalid comment content type: bool",
            context={
                "content_type": type(content).__name__,
                "content_value": str(content)[:100],
            },
            help_text="Comment content supports: str, SafeString, int, float, Decimal, or None. Convert bool to text explicitly if needed.",
            example=cleandoc(
                """
                from stario.html import Comment, render

                render(Comment(str(is_enabled)))
                """
            ),
        )
    elif type(content) is SafeString:
        comment_text = content.safe_str
    elif type(content) is str:
        # Escape the body so ``-->`` stays inert.
        comment_text = escape_text(content)
    elif isinstance(content, (int, float, Decimal)):
        comment_text = str(content)
    else:
        raise StarioError(
            f"Invalid comment content type: {type(content).__name__}",
            context={
                "content_type": type(content).__name__,
                "content_value": str(content)[:100],
            },
            help_text="Comment content supports: str, SafeString, int, float, Decimal, or None.",
            example=cleandoc(
                """
                from stario.html import Comment, render

                render(Comment("Build marker"))
                """
            ),
        )

    return SafeString(f"<!--{comment_text}-->")
