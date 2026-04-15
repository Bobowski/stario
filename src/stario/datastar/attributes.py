"""HTML attribute helpers for Datastar (``data-*`` strings, signals JSON, event bindings).

Each helper returns a small ``dict`` of attributes. Pass those dicts as the first arguments
to a Stario tag (before children): ``h.Input(ds.bind("q"), {"type": "search"})``—the tag
merges consecutive mappings. See the Datastar attributes reference for wire-format details;
docstrings here focus on copy-paste examples (``h`` means ``stario.html`` or its tag imports).
In those blocks, ``#`` lines are the HTML from ``stario.html.render(...)`` (attribute escaping as in the wire format).
"""

import json
from dataclasses import asdict, is_dataclass
from inspect import cleandoc
from typing import Any, Literal

from stario.exceptions import StarioError

from .format import (
    Debounce,
    FilterValue,
    SignalValue,
    Throttle,
    TimeValue,
    debounce_to_string,
    js,
    parse_filter_value,
    s,
    throttle_to_string,
    time_to_string,
    to_kebab_key,
)


def _to_dict(obj: Any) -> dict[str, Any]:
    """
    Convert various types to dict for JSON serialization.

    Supports:
    - dict: returned as-is
    - dataclass instance: converted via asdict()
    - Pydantic model instance: converted via model_dump()
    - Any object with __dict__: uses __dict__
    """
    if isinstance(obj, dict):
        return obj

    # Dataclass instance (not the class itself)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)

    # Pydantic model instance (v2) - check callable to ensure it's a method
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        return obj.model_dump()  # type: ignore[no-any-return]

    # Pydantic model instance (v1 fallback)
    if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
        return obj.dict()  # type: ignore[no-any-return]

    # Generic object with __dict__
    if hasattr(obj, "__dict__"):
        return obj.__dict__

    raise StarioError(
        f"Cannot convert {type(obj).__name__} to signals dict",
        context={"type": type(obj).__name__, "value": repr(obj)[:100]},
        help_text="Pass a dict, dataclass instance, or Pydantic model.",
        example=cleandoc(
            """
            ds.signals({"count": 0})  # dict
            ds.signals(MyDataclass())  # dataclass instance
            ds.signal("count", "0")  # one key on the element
            """
        ),
    )


# DOM event names accepted by ``on()`` (typed hints only; strings also work at runtime).
JSEvent = Literal[
    "abort",
    "afterprint",
    "animationend",
    "animationiteration",
    "animationstart",
    "beforeprint",
    "beforeunload",
    "blur",
    "canplay",
    "canplaythrough",
    "change",
    "click",
    "contextmenu",
    "copy",
    "cut",
    "dblclick",
    "drag",
    "dragend",
    "dragenter",
    "dragleave",
    "dragover",
    "dragstart",
    "drop",
    "durationchange",
    "ended",
    "error",
    "focus",
    "focusin",
    "focusout",
    "fullscreenchange",
    "fullscreenerror",
    "hashchange",
    "input",
    "invalid",
    "keydown",
    "keypress",
    "keyup",
    "load",
    "loadeddata",
    "loadedmetadata",
    "loadstart",
    "message",
    "mousedown",
    "mouseenter",
    "mouseleave",
    "mousemove",
    "mouseover",
    "mouseout",
    "mouseup",
    "mousewheel",
    "offline",
    "online",
    "open",
    "pagehide",
    "pageshow",
    "paste",
    "pause",
    "play",
    "playing",
    "popstate",
    "progress",
    "ratechange",
    "resize",
    "reset",
    "scroll",
    "search",
    "seeked",
    "seeking",
    "select",
    "show",
    "stalled",
    "storage",
    "submit",
    "suspend",
    "timeupdate",
    "toggle",
    "touchcancel",
    "touchend",
    "touchmove",
    "touchstart",
    "transitionend",
    "unload",
    "volumechange",
    "waiting",
    "wheel",
]


def attr(key: str, expression: str) -> dict[str, str]:
    """One ``data-attr:key`` entry (name + expression).

    ```python
    h.Div(ds.attr("title", "$item.label"), {"class": "tooltip"})
    # <div data-attr:title="$item.label" class="tooltip"></div>
    ```
    """
    return {f"data-attr:{key}": expression}


def attrs(mapping: dict[str, str]) -> dict[str, str]:
    """Several custom attrs at once via ``data-attr`` with a JS object literal.

    ```python
    h.Aside(ds.attrs({"open": "sidebarOpen"}), {"class": "drawer"})
    # <aside data-attr="{&#x27;open&#x27;:sidebarOpen}" class="drawer"></aside>
    ```
    """
    return {"data-attr": js(mapping)}


def bind(signal_name: str) -> dict[str, str]:
    """``data-bind`` — two-way bind a signal to an input or similar.

    ```python
    h.Input(ds.bind("email"), {"type": "email", "class": "input input-bordered"})
    # <input data-bind="email" type="email" class="input input-bordered"/>
    ```
    """
    return {"data-bind": signal_name}


def class_(name: str, expression: str) -> dict[str, str]:
    """One ``data-class:name`` entry (class token + expression).

    ```python
    h.Div(ds.class_("hidden", "!$expanded"))
    # <div data-class:hidden="!$expanded"></div>
    ```
    """
    return {f"data-class:{name}": expression}


def classes(mapping: dict[str, str]) -> dict[str, str]:
    """Several class toggles at once via ``data-class`` with a JS object literal.

    ```python
    h.Ul(ds.classes({"loading": "$pending", "text-error": "$error != null"}))
    # <ul data-class="{&#x27;loading&#x27;:$pending,&#x27;text-error&#x27;:$error != null}"></ul>
    ```
    """
    return {"data-class": js(mapping)}


def computed(key: str, expression: str) -> dict[str, str]:
    """One ``data-computed:key`` entry.

    ```python
    h.Span(ds.computed("fullName", "$first + ' ' + $last"))
    # <span data-computed:full-name="$first + &#x27; &#x27; + $last"></span>
    ```
    """
    kebab_key, from_case = to_kebab_key(key)
    if from_case == "camel":
        return {f"data-computed:{kebab_key}": expression}
    return {f"data-computed:{kebab_key}__case.{from_case}": expression}


def computeds(mapping: dict[str, str]) -> dict[str, str]:
    """Several computed fields at once (each key becomes a ``data-computed`` entry).

    ```python
    h.Div(ds.computeds({"fullName": "$first + ' ' + $last", "initials": "$first[0]"}))
    # <div data-computed:full-name="$first + &#x27; &#x27; + $last" data-computed:initials__case.kebab="$first[0]"></div>
    ```
    """
    kebab_cases = [(to_kebab_key(k), value) for k, value in mapping.items()]
    return {
        (
            f"data-computed:{kebab_key}"
            if from_case == "camel"
            else f"data-computed:{kebab_key}__case.{from_case}"
        ): value
        for (kebab_key, from_case), value in kebab_cases
    }


def effect(expression: str) -> dict[str, str]:
    """``data-effect`` — run a client expression when the element is patched.

    ```python
    h.Div(ds.effect("el.querySelector('input')?.focus()"))
    # <div data-effect="el.querySelector(&#x27;input&#x27;)?.focus()"></div>
    ```
    """
    return {"data-effect": expression}


def ignore(self_only: bool = False) -> dict[str, bool]:
    """``data-ignore`` — skip morphing / updates for this subtree (or self only).

    ```python
    h.Div(ds.ignore(), h.P("Third-party widget root"))
    # <div data-ignore><p>Third-party widget root</p></div>
    ```
    """
    return {"data-ignore__self": True} if self_only else {"data-ignore": True}


def ignore_morph() -> dict[str, bool]:
    """``data-ignore-morph`` — keep DOM here from being morphed.

    ```python
    h.Textarea(ds.ignore_morph(), {"name": "notes"})
    # <textarea data-ignore-morph name="notes"></textarea>
    ```
    """
    return {"data-ignore-morph": True}


def indicator(signal_name: str) -> dict[str, str]:
    """``data-indicator`` — bind a signal while fetches run.

    ```python
    h.Span(ds.indicator("saving"), "Saving…")
    # <span data-indicator="saving">Saving…</span>
    ```
    """
    return {"data-indicator": signal_name}


def init(
    expression: str,
    *,
    delay: TimeValue | None = None,
    viewtransition: bool = False,
) -> dict[str, str]:
    """``data-init`` — run once when the element enters the page.

    ```python
    h.Div(ds.init("$focusFirstInput(el)"), {"id": "form-shell"})
    # <div data-init="$focusFirstInput(el)" id="form-shell"></div>
    h.Div(ds.init("loadMore()", delay="200ms"), {"id": "infinite-sentinel"})
    # <div data-init__delay.200ms="loadMore()" id="infinite-sentinel"></div>
    ```
    """
    if delay is None:
        return (
            {"data-init__viewtransition": expression}
            if viewtransition
            else {"data-init": expression}
        )

    mods = "delay." + time_to_string(delay)
    if viewtransition:
        mods += "__viewtransition"
    return {"data-init__" + mods: expression}


def json_signals(
    *,
    include: FilterValue | None = None,
    exclude: FilterValue | None = None,
    terse: bool = False,
) -> dict[str, str | bool]:
    """``data-json-signals`` — control how signals serialize on requests (include/exclude filters).

    ```python
    h.Form(ds.json_signals(include=["email", "password"]), {"action": "/login", "method": "post"})
    # <form data-json-signals="{&#x27;include&#x27;:&#x27;email|password&#x27;}" action="/login" method="post"></form>
    ```
    """
    if include is not None or exclude is not None:
        filters: dict[str, str] = {}
        if include is not None:
            filters["include"] = s(parse_filter_value(include))
        if exclude is not None:
            filters["exclude"] = s(parse_filter_value(exclude))
        value: str | bool = js(filters)
    else:
        value = True

    return {"data-json-signals__terse": value} if terse else {"data-json-signals": value}


def on_intersect(
    expression: str,
    *,
    once: bool = False,
    half: bool = False,
    full: bool = False,
    delay: TimeValue | None = None,
    debounce: Debounce | None = None,
    throttle: Throttle | None = None,
    viewtransition: bool = False,
) -> dict[str, str]:
    """``data-on-intersect`` — Intersection Observer → expression.

    ```python
    h.Div(ds.on_intersect("@get('/feed?cursor=' + $cursor)", once=True), {"id": "sentinel"})
    # <div data-on-intersect__once="@get(&#x27;/feed?cursor=&#x27; + $cursor)" id="sentinel"></div>
    ```
    """
    modifiers: list[str] = []
    append = modifiers.append
    if once:
        append("once")
    if half:
        append("half")
    if full:
        append("full")
    if delay is not None:
        append("delay." + time_to_string(delay))
    if debounce is not None:
        append(debounce_to_string(debounce))
    if throttle is not None:
        append(throttle_to_string(throttle))
    if viewtransition:
        append("viewtransition")

    return (
        {"data-on-intersect__" + "__".join(modifiers): expression}
        if modifiers
        else {"data-on-intersect": expression}
    )


def on_interval(
    expression: str,
    *,
    duration: TimeValue | tuple[TimeValue, Literal["leading"]] = "1s",
    viewtransition: bool = False,
) -> dict[str, str]:
    """``data-on-interval`` — timer-driven expression.

    ```python
    h.Div(ds.on_interval("$pollInbox()", duration="5s"))
    # <div data-on-interval__duration.5s="$pollInbox()"></div>
    ```
    """
    if duration == "1s":
        return (
            {"data-on-interval__viewtransition": expression}
            if viewtransition
            else {"data-on-interval": expression}
        )

    if isinstance(duration, (int, float, str)):
        mods = "duration." + time_to_string(duration)
    elif isinstance(duration, tuple):
        mods = f"duration.{time_to_string(duration[0])}.{duration[1]}"
    else:
        raise StarioError(
            f"Invalid duration configuration for on_interval: {duration}",
            context={
                "duration_value": str(duration),
                "duration_type": type(duration).__name__,
            },
            help_text="Duration must be a time value (int/float/str) or a tuple with time and 'leading' modifier.",
        )

    if viewtransition:
        mods += "__viewtransition"
    return {"data-on-interval__" + mods: expression}


def on_signal_patch(
    expression: str,
    *,
    delay: TimeValue | None = None,
    debounce: Debounce | None = None,
    throttle: Throttle | None = None,
    include: FilterValue | None = None,
    exclude: FilterValue | None = None,
) -> dict[str, str]:
    """``data-on-signal-patch`` — react to signal changes (optional include/exclude filters).

    ```python
    h.Div(ds.on_signal_patch("@post('/autosave')", debounce="500ms", include=["draft"]))
    # <div data-on-signal-patch__debounce.500ms="@post(&#x27;/autosave&#x27;)" data-on-signal-patch-filter="{&#x27;include&#x27;:&#x27;draft&#x27;}"></div>
    ```
    """
    modifiers: list[str] = []
    append = modifiers.append
    if delay is not None:
        append("delay." + time_to_string(delay))
    if debounce is not None:
        append(debounce_to_string(debounce))
    if throttle is not None:
        append(throttle_to_string(throttle))

    key = (
        "data-on-signal-patch__" + "__".join(modifiers)
        if modifiers
        else "data-on-signal-patch"
    )

    if include is not None or exclude is not None:
        filter_dict: dict[str, str] = {}
        if include is not None:
            filter_dict["include"] = s(parse_filter_value(include))
        if exclude is not None:
            filter_dict["exclude"] = s(parse_filter_value(exclude))
        return {
            key: expression,
            "data-on-signal-patch-filter": js(filter_dict),
        }

    return {key: expression}


def on(
    event: JSEvent | str,
    expression: str,
    *,
    once: bool = False,
    passive: bool = False,
    capture: bool = False,
    delay: TimeValue | None = None,
    debounce: Debounce | None = None,
    throttle: Throttle | None = None,
    viewtransition: bool = False,
    window: bool = False,
    outside: bool = False,
    prevent: bool = False,
    stop: bool = False,
) -> dict[str, str]:
    """``data-on:*`` — DOM (or window) events with optional modifiers.

    ```python
    h.Button(ds.on("click", ds.get("/cart/count")), {"type": "button", "class": "btn"})
    # <button data-on:click="@get(&#x27;/cart/count&#x27;)" type="button" class="btn"></button>
    h.Input(ds.on("keydown", "$query = el.value", debounce=("150ms", "leading")))
    # <input data-on:keydown__debounce.150ms.leading="$query = el.value"/>
    ```
    """
    modifiers: list[str] = []
    append = modifiers.append
    if once:
        append("once")
    if passive:
        append("passive")
    if capture:
        append("capture")
    if window:
        append("window")
    if outside:
        append("outside")
    if prevent:
        append("prevent")
    if stop:
        append("stop")
    if delay is not None:
        append("delay." + time_to_string(delay))
    if debounce is not None:
        append(debounce_to_string(debounce))
    if throttle is not None:
        append(throttle_to_string(throttle))
    if viewtransition:
        append("viewtransition")

    kebab_event, from_case = to_kebab_key(event)
    if from_case != "kebab":
        append("case." + from_case)

    return (
        {f"data-on:{kebab_event}__{'__'.join(modifiers)}": expression}
        if modifiers
        else {f"data-on:{kebab_event}": expression}
    )


def preserve_attr(attrs: str | list[str]) -> dict[str, str]:
    """``data-preserve-attr`` — attribute names to keep across morphs.

    ```python
    h.Div(ds.preserve_attr(["data-testid", "id"]), {"class": "card"})
    # <div data-preserve-attr="data-testid id" class="card"></div>
    ```
    """
    value = attrs if isinstance(attrs, str) else " ".join(attrs)
    return {"data-preserve-attr": value}


def ref(signal_name: str) -> dict[str, str]:
    """``data-ref`` — store the element on a signal (e.g. for focus helpers).

    ```python
    h.Input(ds.ref("searchInput"), ds.bind("q"), {"type": "search"})
    # <input data-ref="searchInput" data-bind="q" type="search"/>
    ```
    """
    return {"data-ref": signal_name}


def show(expression: str) -> dict[str, str]:
    """``data-show`` — visibility from a boolean expression.

    ```python
    h.Div({"class": "alert alert-error", "role": "alert"}, ds.show("$error != null"))
    # <div class="alert alert-error" role="alert" data-show="$error != null"></div>
    ```
    """
    return {"data-show": expression}


def signal(name: str, expression: str, *, ifmissing: bool = False) -> dict[str, str]:
    """One ``data-signals:key`` entry on this element.

    ```python
    h.Div({"class": "theme-root"}, ds.signal("theme", "'dark'"))
    # <div class="theme-root" data-signals:theme__case.kebab="&#x27;dark&#x27;"></div>
    ```
    """
    kebab_key, from_case = to_kebab_key(name)
    mods = ""
    if from_case != "camel":
        mods += f"__case.{from_case}"
    if ifmissing:
        mods += "__ifmissing"
    return {f"data-signals:{kebab_key}{mods}": expression}


def signals(
    data: dict[str, SignalValue] | Any,
    *,
    ifmissing: bool = False,
) -> dict[str, str]:
    """Initial JSON signals object from a dict, dataclass instance, or similar (``data-signals``).

    ```python
    h.Body(ds.signals({"count": 0, "open": False}), h.Main(...))
    # <body data-signals="{&quot;count&quot;:0,&quot;open&quot;:false}"><main>…</main></body>
    ```
    """
    if isinstance(data, str):
        raise TypeError(
            "signals() expects a dict or model object; use signal(name, expression) for one key."
        )
    signals_dict = _to_dict(data)
    attr_key = "data-signals__ifmissing" if ifmissing else "data-signals"
    return {
        attr_key: json.dumps(signals_dict, separators=(",", ":"), ensure_ascii=False)
    }


def style(prop: str, expression: str) -> dict[str, str]:
    """One ``data-style:property`` entry.

    ```python
    h.Div({"class": "bar-fill"}, ds.style("width", "$pct + '%'"))
    # <div class="bar-fill" data-style:width="$pct + &#x27;%&#x27;"></div>
    ```
    """
    return {f"data-style:{prop}": expression}


def styles(mapping: dict[str, str]) -> dict[str, str]:
    """Several style properties at once via ``data-style`` with a JS object literal.

    ```python
    h.Div(ds.styles({"opacity": "$visible ? '1' : '0'"}))
    # <div data-style="{&#x27;opacity&#x27;:$visible ? &#x27;1&#x27; : &#x27;0&#x27;}"></div>
    ```
    """
    return {"data-style": js(mapping)}


def text(expression: str) -> dict[str, str]:
    """``data-text`` — text content from an expression.

    ```python
    h.P(ds.text("$greeting"))
    # <p data-text="$greeting"></p>
    h.Span({"class": "font-mono"}, ds.text("$user.name"))
    # <span class="font-mono" data-text="$user.name"></span>
    ```
    """
    return {"data-text": expression}
