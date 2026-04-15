"""
Compile repeated HTML layouts with the ``@baked`` decorator.

Without ``@baked``, each call to your builder constructs a fresh tree (new tag
tuples and children) and ``render`` walks that entire tree again to produce a
string.

With ``@baked``, most of that work happens once when the decorator runs (usually
at import):

1. Capture: your function runs with an internal placeholder per parameter, so
   the tree matches what plain tags would build.
2. Flatten: that tree becomes an immutable segment plan. Consecutive static
   pieces merge into longer ``SafeString`` chunks; each parameter is a slot at
   fixed indices.
3. Call: each real call copies the plan, splices argument nodes into those
   slots, and returns a fragment (a ``list`` of nodes, or one ``SafeString``
   when there are no parameters). Pass that to ``render`` as a single root, or
   nest it like any other element child.

This is not a template language: dynamic values must be children of elements,
not values inside attribute dicts. Parameters must be a fixed set of normal or
keyword-only names (no ``/``, ``*args``, or ``**kwargs``).

Zero parameters: the whole layout is frozen into one ``SafeString``; each call
returns that cached string. Use ``render(wrap())`` when you need a ``str``, or
embed it as child content.

Rendering: use ``render(layout(...))``; ``render`` walks into list fragments, so
star-unpacking is unnecessary. For zero-argument layouts, ``render(layout())`` or
embed the ``SafeString`` in a larger tree.

Purely positional calls avoid ``inspect.Signature.bind``.

Examples:

    from stario.html import Div, P, Title, baked, render

    @baked
    def page(title, body):
        return Div(Title(title), body)

    html = render(page("Docs", P("Hello.")))

    @baked
    def page_kw(*, title, body):
        return Div(Title(title), body)

    html = render(page_kw(title="Docs", body=P("Hello.")))

    @baked
    def chrome():
        return Div({"class": "shell"}, P("fixed nav"))

    html = render(chrome())
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
from typing import Any, cast, overload

from stario.exceptions import StarioError

from .escape import escape_text
from .tag import Tag
from .types import HtmlElement, HtmlElementTuple, SafeString


@dataclass(frozen=True, slots=True)
class _BakeSlot:
    """One ``@baked`` placeholder."""

    name: str


type BakedNode = HtmlElement | _BakeSlot
type PlanSegment = SafeString | _BakeSlot
type BakedFragment = SafeString | list[HtmlElement]


# --- signature (only fixed parameter lists are supported) -------------------


def _reject_unsupported_parameter_kinds(sig: inspect.Signature) -> None:
    for param_name, param in sig.parameters.items():
        match param.kind:
            case inspect.Parameter.POSITIONAL_ONLY:
                detail = "positional-only parameters are not allowed (no `/`)"
            case inspect.Parameter.VAR_POSITIONAL:
                detail = "*args is not allowed"
            case inspect.Parameter.VAR_KEYWORD:
                detail = "**kwargs is not allowed"
            case _:
                continue
        raise StarioError(
            f"@baked: {detail}",
            context={"parameter": param_name},
            help_text="Use a fixed set of normal or keyword-only parameters.",
        )


# --- flatten tree → immutable segment tuple ---------------------------------


def _flush_pending_static_text(
    pending_static_chars: list[str], segments_out: list[PlanSegment]
) -> None:
    if pending_static_chars:
        segments_out.append(SafeString("".join(pending_static_chars)))
        pending_static_chars.clear()


def _flatten_node_into_segments(
    node: BakedNode | None,
    pending_static_chars: list[str],
    segments_out: list[PlanSegment],
) -> None:
    if node is None:
        return

    match node:
        case _BakeSlot():
            _flush_pending_static_text(pending_static_chars, segments_out)
            segments_out.append(node)

        case SafeString():
            pending_static_chars.append(node.safe_str)
        case str():
            pending_static_chars.append(escape_text(node))
        case bool():
            raise StarioError(
                "@baked: boolean values are not valid HTML child content",
                context={"node_value": str(node)},
                help_text="Use a conditional to include or omit content, or convert the value to text explicitly with str(...).",
            )
        case int() | float() | Decimal():
            pending_static_chars.append(str(node))

        case Tag():
            raise StarioError(
                "@baked: cannot capture a Tag object directly",
                context={"tag": repr(node)},
                help_text="Call the tag first so the builder returns an element, e.g. use Div() instead of Div.",
            )

        case tuple() as element_tuple:
            open_html, children, close_html = cast(HtmlElementTuple, element_tuple)
            pending_static_chars.append(open_html)
            for child in children:
                _flatten_node_into_segments(child, pending_static_chars, segments_out)
            pending_static_chars.append(close_html)

        case list() as child_list:
            for child in cast(list[BakedNode], child_list):
                _flatten_node_into_segments(child, pending_static_chars, segments_out)

        case _:
            raise StarioError(
                f"@baked: unsupported node type: {type(node).__name__}",
                context={"node_type": type(node).__name__},
                help_text="Use tags, text, lists, tuples, SafeString, and parameters only.",
            )


def _frozen_segment_plan(root: HtmlElement) -> tuple[PlanSegment, ...]:
    pending: list[str] = []
    segments: list[PlanSegment] = []

    match root:
        case list() as roots:
            for node in cast(list[HtmlElement], roots):
                _flatten_node_into_segments(node, pending, segments)
        case _:
            _flatten_node_into_segments(root, pending, segments)

    _flush_pending_static_text(pending, segments)
    return tuple(segments)


def _parameter_name_to_slot_indices(
    plan: tuple[PlanSegment, ...],
) -> dict[str, tuple[int, ...]]:
    """Map each parameter name to every index in ``plan`` holding that slot."""
    lists: dict[str, list[int]] = {}
    for index, segment in enumerate(plan):
        if type(segment) is _BakeSlot:
            slot = cast(_BakeSlot, segment)
            lists.setdefault(slot.name, []).append(index)
    return {name: tuple(indices) for name, indices in lists.items()}


# --- per-call argument resolution --------------------------------------------


def _arguments_from_positional_only(
    sig: inspect.Signature, positional_args: tuple[Any, ...]
) -> dict[str, Any]:
    """
    Map ``positional_args`` to parameter names without ``inspect.bind``.

    Valid only for signatures that passed ``_reject_unsupported_parameter_kinds``.
    Caller must ensure ``kwargs`` is empty when using this.
    """
    resolved: dict[str, Any] = {}
    arg_index = 0
    num_positional = len(positional_args)
    for param_name, param in sig.parameters.items():
        match param.kind:
            case inspect.Parameter.POSITIONAL_OR_KEYWORD:
                if arg_index < num_positional:
                    resolved[param_name] = positional_args[arg_index]
                    arg_index += 1
                elif param.default is not inspect.Parameter.empty:
                    resolved[param_name] = param.default
                else:
                    raise TypeError(f"missing required argument {param_name!r}")
            case inspect.Parameter.KEYWORD_ONLY:
                if param.default is not inspect.Parameter.empty:
                    resolved[param_name] = param.default
                else:
                    raise TypeError(
                        f"missing required keyword-only argument {param_name!r}"
                    )
            case _:
                raise RuntimeError(f"unexpected parameter kind: {param.kind!r}")
    if arg_index < num_positional:
        raise TypeError(
            f"too many positional arguments ({num_positional} given, {arg_index} consumed)"
        )
    return resolved


def _resolve_arguments(
    sig: inspect.Signature,
    positional_args: tuple[Any, ...],
    keyword_args: dict[str, Any],
) -> dict[str, Any]:
    if keyword_args:
        bound = sig.bind(*positional_args, **keyword_args)
        bound.apply_defaults()
        return bound.arguments
    return _arguments_from_positional_only(sig, positional_args)


# --- public decorator --------------------------------------------------------


@overload
def baked[**P](fn: Callable[P, HtmlElement]) -> Callable[P, BakedFragment]: ...


@overload
def baked[**P](
    fn: None = None,
) -> Callable[[Callable[P, HtmlElement]], Callable[P, BakedFragment]]: ...


def baked[**P](
    fn: Callable[P, HtmlElement] | None = None,
) -> (
    Callable[[Callable[P, HtmlElement]], Callable[P, BakedFragment]]
    | Callable[P, BakedFragment]
):
    """
    Compile ``fn`` once into a segment plan, then return a fast callable.

    On decoration, ``fn`` runs with placeholder arguments so static markup is
    flattened to ``SafeString`` runs and each parameter gets reserved indices in
    the plan. Each invocation copies that plan and writes real argument nodes
    into those slots. With no parameters, the plan is a single precomputed
    ``SafeString`` returned on every call.

    Dynamic parameter values must appear as element children (not inside
    attribute mappings). See the module docstring for constraints and examples.
    """

    def decorate(builder: Callable[P, HtmlElement]) -> Callable[P, BakedFragment]:
        sig = inspect.signature(builder)
        _reject_unsupported_parameter_kinds(sig)

        # Build the layout once with placeholders in every parameter slot.
        placeholder_kwargs = {name: _BakeSlot(name) for name in sig.parameters}
        try:
            captured_tree = cast(Callable[..., HtmlElement], builder)(**placeholder_kwargs)
        except TypeError as error:
            raise StarioError(
                f"@baked: cannot call {builder.__qualname__!r} with placeholders: {error}",
                context={"function": builder.__qualname__},
            ) from error
        except StarioError:
            raise
        except Exception as error:
            raise StarioError(
                f"@baked: building {builder.__qualname__!r} failed: {type(error).__name__}: {error}",
                context={"function": builder.__qualname__},
            ) from error

        if captured_tree is None:
            raise StarioError(
                f"@baked: {builder.__qualname__!r} returned None while building",
                context={"function": builder.__qualname__},
            )

        # Freeze the static parts once, then patch the dynamic slots on each call.
        frozen_plan = _frozen_segment_plan(captured_tree)
        if not sig.parameters:
            compiled_static = SafeString(
                "".join(cast(SafeString, segment).safe_str for segment in frozen_plan)
            )

            @wraps(builder)
            def static_wrapper(*call_args: P.args, **call_kwargs: P.kwargs) -> SafeString:
                if call_args or call_kwargs:
                    sig.bind(*call_args, **call_kwargs)
                return compiled_static

            static_wrapper.__signature__ = sig  # type: ignore[attr-defined]
            return static_wrapper

        slot_indices_by_parameter = _parameter_name_to_slot_indices(frozen_plan)

        @wraps(builder)
        def dynamic_wrapper(*call_args: P.args, **call_kwargs: P.kwargs) -> list[HtmlElement]:
            arguments_by_name = _resolve_arguments(sig, call_args, call_kwargs)

            rendered_row: list[BakedNode] = list(frozen_plan)
            for parameter_name, indices in slot_indices_by_parameter.items():
                value = cast(HtmlElement, arguments_by_name[parameter_name])
                for plan_index in indices:
                    rendered_row[plan_index] = value

            return cast(list[HtmlElement], rendered_row)

        dynamic_wrapper.__signature__ = sig  # type: ignore[attr-defined]
        return dynamic_wrapper

    if fn is not None:
        return decorate(fn)
    return decorate
