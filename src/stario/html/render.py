"""Render HTML trees."""

from collections.abc import Callable, Iterable
from decimal import Decimal
from inspect import cleandoc
from typing import cast

from stario.exceptions import StarioError

from .baked import _BakeSlot
from .escape import escape_text
from .tag import Tag
from .types import HtmlElement, HtmlElementTuple, SafeString

type RenderNode = HtmlElement | _BakeSlot


def _render(nodes: Iterable[RenderNode], append: Callable[[str], None]) -> None:
    """Depth-first render."""
    for node in nodes:
        if node is None:
            continue

        node_type = type(node)

        # Internal placeholders should never survive to the final render step.
        if node_type is _BakeSlot:
            raise StarioError(
                "Unfilled @baked slot in render tree",
                context={"slot_name": cast(_BakeSlot, node).name},
                help_text="Call the baked function to substitute slots, then render the returned fragment.",
                example=cleandoc(
                    """
                    from stario.html import Div, P, Title, baked, render

                    @baked
                    def layout(title, body):
                        return Div(Title(title), body)

                    render(layout("Hi", P("x")))
                    """
                ),
            )

        if node_type is tuple:
            node_tuple = cast(tuple[object, ...], node)
            try:
                start, children, end = cast(HtmlElementTuple, node)
            except ValueError:
                raise StarioError(
                    f"Invalid tuple length for HTML element: {len(node_tuple)}",
                    context={
                        "node_type": type(node).__name__,
                        "node_value": str(node)[:100],
                    },
                    help_text="HTML elements must be tuples with three elements: start tag, children, and end tag.",
                    example=cleandoc(
                        """
                        from stario.html import Div, P, render

                        render(Div({"class": "container"}, P("Hello")))
                        """
                    ),
                )

            append(start)
            _render(children, append)
            append(end)
            continue

        # Plain values render directly.
        if node_type is str:
            append(escape_text(cast(str, node)))
            continue

        if node_type is list:
            _render(cast(list[HtmlElement], node), append)
            continue

        if node_type is SafeString:
            append(cast(SafeString, node).safe_str)
            continue

        if node_type is Tag:
            raise StarioError(
                "Cannot render a Tag object directly",
                context={"tag": repr(node)},
                help_text="Call the tag first to create an element, e.g. use Div() instead of Div.",
                example=cleandoc(
                    """
                    from stario.html import Div, render

                    render(Div("Hello"))
                    """
                ),
            )

        if node_type is bool:
            raise StarioError(
                "Boolean values are not valid HTML child content",
                context={"node_value": str(node)},
                help_text="Use a conditional to include or omit content, or convert the value to text explicitly with str(...).",
                example=cleandoc(
                    """
                    from stario.html import P

                    P("enabled" if is_enabled else "disabled")
                    """
                ),
            )

        if node_type is int or node_type is float or node_type is Decimal:
            append(str(node))
            continue

        raise StarioError(
            f"Cannot render element of type {type(node).__name__}",
            context={
                "node_type": type(node).__name__,
                "node_value": str(node)[:100],
            },
            help_text="Only str, int, float, Decimal, SafeString, lists, and element tuples can be rendered.",
            example=cleandoc(
                """
                from stario.html import Div, P, SafeString

                Div(str(my_object))
                """
            ),
        )


def render(*nodes: HtmlElement) -> str:
    """Walk HTML fragments depth-first and return one UTF-8 string.

    Accepts any number of root nodes (variadic). Plain ``str`` in child position
    is HTML text-escaped; attribute escaping happens inside ``Tag`` when the
    opening tag is built. ``SafeString`` is emitted unchanged (trusted).

    Fragments from ``baked`` are a ``list`` of nodes (or a single ``SafeString``
    when there are no parameters). Pass them as one root, e.g.
    ``render(layout(a, b))``: lists are flattened while walking the tree. You can
    also nest ``layout(...)`` wherever an element child is accepted, same as
    ``Div(...)``.
    """
    try:
        results: list[str] = []
        _render(nodes, results.append)
        return "".join(results)

    except StarioError:
        raise
    except Exception as e:
        raise StarioError(
            f"Unexpected error while rendering HTML: {type(e).__name__}: {e}",
            context={
                "error_type": type(e).__name__,
                "error_message": str(e),
                "node_count": len(nodes) if hasattr(nodes, "__len__") else "unknown",
            },
            help_text="Check that all HTML elements are valid types and properly structured.",
            example=cleandoc(
                """
                from stario.html import Div, P, render

                render(Div({"class": "container"}, P("Hello")))
                """
            ),
        ) from e
