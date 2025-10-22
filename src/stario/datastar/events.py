import json
from dataclasses import dataclass
from typing import (
    AsyncGenerator,
    Callable,
    Final,
    Literal,
    cast,
    get_args,
)

from stario.html import TagAttributes, render, script

type PatchMode = Literal[
    "outer",
    "inner",
    "replace",
    "prepend",
    "append",
    "before",
    "after",
    "remove",
]
PatchModes: Final[frozenset[PatchMode]] = frozenset(get_args(PatchMode))
type PatchSelector = str
type SignalValue = str | int | float | bool | list[SignalValue] | dict[str, SignalValue]
type SignalsDict = dict[str, SignalValue]

# Server-Sent Events (SSE) related
type PatchSignalsEvent = SignalsPatch | SignalsDict
type PatchElementsEvent[ElementT] = (
    ElementsPatch[ElementT]
    | ElementT
    | tuple[PatchMode, PatchSelector, ElementT]
    | tuple[PatchMode, ElementT]
    | tuple[Literal["remove"], PatchSelector]
    | tuple[Literal["script"], str]
    | tuple[Literal["redirect"], str]
)
type Event[ElementT] = (
    PatchElementsEvent[ElementT] | PatchSignalsEvent | ScriptExecution | Redirection
)
type EventStream[ElementT] = AsyncGenerator[Event[ElementT], None]


@dataclass(slots=True)
class ElementsPatch[ElementT]:
    """
    https://data-star.dev/reference/sse_events#datastar-patch-elements
    """

    mode: PatchMode = "outer"
    selector: PatchSelector | None = None
    elements: ElementT | None = None
    use_view_transition: bool = False

    # SSE specific
    event_id: str | None = None
    retry_duration: int | None = None

    def to_sse(self, renderer: Callable[[ElementT], str]) -> str:
        # Standard SSE headers
        lines = ["event: datastar-patch-elements"]
        append = lines.append

        if self.event_id:
            append(f"id: {self.event_id}")

        if self.retry_duration and self.retry_duration != 1000:
            append(f"retry: {self.retry_duration}")

        # Datastar specific:
        if self.mode and self.mode != "outer":
            append(f"data: mode {self.mode}")

        if self.selector:
            append(f"data: selector {self.selector}")

        if self.use_view_transition:
            append(f"data: useViewTransition {self.use_view_transition}")

        if self.elements:

            # Render the element using the renderer
            element = renderer(self.elements)

            # Split elements into lines - this should be faster than splitlines()
            start = 0
            while True:
                end = element.find("\n", start)
                if end == -1:
                    append(f"data: elements {element[start:]}")
                    break
                append(f"data: elements {element[start:end]}")
                start = end + 1

        return "\n".join(lines) + "\n\n"


@dataclass(slots=True)
class SignalsPatch:
    """
    https://data-star.dev/reference/sse_events#datastar-patch-signals
    """

    signals: SignalsDict
    only_if_missing: bool = False

    # SSE specific
    event_id: str | None = None
    retry_duration: int | None = None

    def to_sse(self) -> str:

        # Standard SSE headers
        lines = ["event: datastar-patch-signals"]
        append = lines.append

        if self.event_id:
            append(f"id: {self.event_id}")

        if self.retry_duration and self.retry_duration != 1000:
            append(f"retry: {self.retry_duration}")

        if self.only_if_missing:
            js_bool = "true" if self.only_if_missing else "false"
            append(f"data: onlyIfMissing {js_bool}")

        # Datastar specific:
        # Split json into lines - this should be faster than splitlines()
        json_str = json.dumps(self.signals)
        start = 0
        while True:
            end = json_str.find("\n", start)
            if end == -1:
                append(f"data: signals {json_str[start:]}")
                break
            append(f"data: signals {json_str[start:end]}")
            start = end + 1

        return "\n".join(lines) + "\n\n"


@dataclass(slots=True)
class ElementsRemoval[ElementT]:

    selector: PatchSelector | None = None
    elements: ElementT | None = None

    # SSE specific
    event_id: str | None = None
    retry_duration: int | None = None

    def to_sse(self, renderer: Callable[[ElementT], str]) -> str:
        return ElementsPatch(
            mode="remove",
            selector=self.selector,
            elements=self.elements,
            event_id=self.event_id,
            retry_duration=self.retry_duration,
        ).to_sse(renderer)


@dataclass(slots=True)
class ScriptExecution:

    script: str
    auto_remove: bool = True
    attributes: TagAttributes | None = None

    # SSE specific
    event_id: str | None = None
    retry_duration: int | None = None

    def to_sse(self) -> str:

        return ElementsPatch(
            mode="append",
            selector="body",
            elements=script(
                self.attributes or {},
                {"data-effect": "el.remove();"} if self.auto_remove else {},
                self.script,
            ),
            event_id=self.event_id,
            retry_duration=self.retry_duration,
        ).to_sse(render)


@dataclass(slots=True)
class Redirection:

    location: str

    # SSE specific
    event_id: str | None = None
    retry_duration: int | None = None

    def to_sse(self) -> str:
        script = f"setTimeout(() => window.location = '{self.location}');"
        return ScriptExecution(
            script=script,
            event_id=self.event_id,
            retry_duration=self.retry_duration,
        ).to_sse()


def patch_to_sse[ElementT](
    patch: Event[ElementT],
    renderer: Callable[[ElementT], str],
) -> str:

    if isinstance(patch, (SignalsPatch, ScriptExecution, Redirection)):
        # yield SignalsPatch or ScriptExecution or Redirection directly
        return patch.to_sse()

    if isinstance(patch, (ElementsPatch, ElementsRemoval)):
        # yield ElementsPatch or ElementsRemoval directly
        return patch.to_sse(renderer)

    if isinstance(patch, dict):
        # yield {"signal1": "value1", "signal2": "value2"}
        return SignalsPatch(signals=patch).to_sse()

    if isinstance(patch, tuple):

        if len(patch) == 3 and patch[0] in PatchModes:
            # yield (PatchMode, PatchSelector, ElementT)
            return ElementsPatch(
                mode=patch[0], selector=patch[1], elements=patch[2]
            ).to_sse(renderer)

        if len(patch) == 2:

            first, second = patch

            if first == "script" and isinstance(second, str):
                # yield ("script", str)
                return ScriptExecution(script=second).to_sse()

            if first == "redirect" and isinstance(second, str):
                # yield ("redirect", str)
                return Redirection(location=second).to_sse()

            if first in PatchModes:
                # yield ("remove", PatchSelector)
                # or yield (PatchMode, ElementT)

                if (
                    first == "remove"
                    and isinstance(second, str)
                    and not (second.startswith("<") and second.endswith(">"))
                ):
                    # Special case when removing by providing a selector string (not HTML elements)
                    return ElementsRemoval(selector=second).to_sse(renderer)

                # yield (PatchMode, ElementT)
                second = cast(ElementT, second)
                return ElementsPatch(mode=first, elements=second).to_sse(renderer)

    # yield (elements) # HTML elements
    # We assume anything else is a HTML element that will be rendered by provided renderer
    return ElementsPatch[ElementT](elements=cast(ElementT, patch)).to_sse(renderer)
