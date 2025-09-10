import json
from dataclasses import dataclass
from typing import Annotated, AsyncGenerator, Literal, Protocol, runtime_checkable

from pydantic import TypeAdapter
from starlette.requests import Request


@runtime_checkable
class _HtmlProvider(Protocol):
    """A type that produces text ready to be placed in an HTML document.

    This is a convention used by html producing/consuming libraries. This lets
    e.g. fasthtml fasttags, or htpy elements, be passed straight in to
    merge_fragments.
    """

    def __html__(self) -> str: ...


@runtime_checkable
class _SSEProvider(Protocol):
    """
    A type that can be converted to a SSE string.
    """

    def to_sse(self) -> str: ...


type HtmlElement = str | _HtmlProvider
type HtmlElements = list[HtmlElement] | HtmlElement
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
type PatchSelector = str
type SignalValue = str | int | float | bool | list[SignalValue] | dict[str, SignalValue]
type SignalsDict = dict[str, SignalValue]

# SSE related
type PatchSignalsEvent = (PatchSignals | SignalsDict | tuple[SignalsDict, bool])
type PatchElementsEvent = (
    PatchElements
    | HtmlElement
    | tuple[PatchMode, PatchSelector, HtmlElement]
    | tuple[PatchMode, HtmlElement]
    | tuple[Literal["remove"], PatchSelector]
)
type PatchEvent = PatchElementsEvent | PatchSignalsEvent | ExecuteScript | Redirect
type PatchGenerator = AsyncGenerator[PatchEvent, None]


@dataclass(slots=True)
class PatchElements:
    """
    https://data-star.dev/reference/sse_events#datastar-patch-elements
    """

    mode: PatchMode = "outer"
    selector: PatchSelector | None = None
    elements: HtmlElements | None = None
    use_view_transition: bool = False

    # SSE specific
    event_id: str | None = None
    retry_duration: int | None = None

    def to_sse(self) -> str:
        # Standard SSE headers
        lines = ["event: datastar-patch-elements"]

        if self.event_id:
            lines.append(f"id: {self.event_id}")

        if self.retry_duration and self.retry_duration != 1000:
            lines.append(f"retry: {self.retry_duration}")

        # Datastar specific:
        if self.mode and self.mode != "outer":
            lines.append(f"data: mode {self.mode}")

        if self.selector:
            lines.append(f"data: selector {self.selector}")

        if self.use_view_transition:
            lines.append(f"data: useViewTransition {self.use_view_transition}")

        if self.elements:

            if not isinstance(self.elements, list):
                elements = [self.elements]
            else:
                elements = self.elements

            for element in elements:

                if isinstance(element, _HtmlProvider):
                    element = element.__html__()

                # Split elements into lines - this should be faster than splitlines()
                start = 0
                while True:
                    end = element.find("\n", start)
                    if end == -1:
                        lines.append(f"data: elements {element[start:]}")
                        break
                    lines.append(f"data: elements {element[start:end]}")
                    start = end + 1

        return "\n".join(lines) + "\n\n"


@dataclass(slots=True)
class PatchSignals:
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

        if self.event_id:
            lines.append(f"id: {self.event_id}")

        if self.retry_duration and self.retry_duration != 1000:
            lines.append(f"retry: {self.retry_duration}")

        if self.only_if_missing:
            js_bool = "true" if self.only_if_missing else "false"
            lines.append(f"data: onlyIfMissing {js_bool}")

        # Datastar specific:
        # Split json into lines - this should be faster than splitlines()
        json_str = json.dumps(self.signals)
        start = 0
        while True:
            end = json_str.find("\n", start)
            if end == -1:
                lines.append(f"data: signals {json_str[start:]}")
                break
            lines.append(f"data: signals {json_str[start:end]}")
            start = end + 1

        return "\n".join(lines) + "\n\n"


@dataclass(slots=True)
class RemoveElements:

    selector: PatchSelector | None = None
    elements: HtmlElements | None = None

    def to_sse(self) -> str:
        return PatchElements(
            mode="remove",
            selector=self.selector,
            elements=self.elements,
        ).to_sse()


@dataclass(slots=True)
class ExecuteScript:

    script: str
    auto_remove: bool = True
    # attributes: Mapping[str, str] | list[str] | None = None

    def to_sse(self) -> str:
        attribute_string = ""
        if self.auto_remove:
            attribute_string += ' data-effect="el.remove()"'

        return PatchElements(
            mode="append",
            selector="body",
            elements=f"<script{attribute_string}>{self.script}</script>",
        ).to_sse()


@dataclass(slots=True)
class Redirect:

    location: str

    def to_sse(self) -> str:
        script = f"setTimeout(() => window.location = '{self.location}')"
        return ExecuteScript(script=script).to_sse()


# TODO: Attribute builder from datastar_py

# Datastar quick responses for streaming over sse


def patch_to_sse(patch: PatchEvent) -> str:

    if isinstance(patch, _SSEProvider):
        return patch.to_sse()

    if isinstance(patch, dict):
        return PatchSignals(signals=patch).to_sse()

    if isinstance(patch, (str, _HtmlProvider)):
        return PatchElements(elements=[patch]).to_sse()

    if isinstance(patch, tuple):
        if len(patch) == 3:
            return PatchElements(
                mode=patch[0],
                selector=patch[1],
                elements=patch[2],
            ).to_sse()

        if len(patch) == 2:

            first, second = patch

            if isinstance(first, dict):
                return PatchSignals(
                    signals=first, only_if_missing=bool(second)
                ).to_sse()

            if first != "remove":
                # Accept strings or HTML providers. Disallow bool which can be confused with int.
                if isinstance(second, bool):
                    raise ValueError(f"Unknown patch tuple: {patch}")
                return PatchElements(mode=first, elements=second).to_sse()

            if isinstance(second, str):
                if second.startswith("<") and second.endswith(">"):
                    return PatchElements(mode=first, elements=second).to_sse()

                return PatchElements(mode=first, selector=second).to_sse()

            if isinstance(second, bool):
                raise ValueError(f"Unknown patch tuple: {patch}")

            return PatchElements(mode=first, elements=second).to_sse()

        raise ValueError(f"Unknown patch tuple: {patch}")

    raise ValueError(f"Unknown patch type: {type(patch)}")


LOAD_DATASTAR = '<script type="module" src="https://cdn.jsdelivr.net/gh/starfederation/datastar@main/bundles/datastar.js"></script>'


class SignalsParam:

    def __init__(self, name: str = "datastar"):
        self.name = name
        self.adapter = TypeAdapter(SignalsDict)

    async def __call__(self, request: Request) -> SignalsDict:
        if request.method == "GET":
            raw = request.query_params.get(self.name)
        else:
            raw = await request.body()

        if raw is None:
            return {}

        return self.adapter.validate_json(raw)


type Signals = Annotated[SignalsDict, SignalsParam()]
