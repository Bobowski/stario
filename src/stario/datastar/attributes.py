"""
Datastar attribute builders (data-* helpers).

This module defines `DatastarAttributes` and the JS event literal type used by `on()`.
The package-level public exports live in `stario.datastar` (`datastar/__init__.py`).
"""

import json
from dataclasses import asdict, is_dataclass
from typing import Any, Literal, overload

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
        example='data.signals({"count": 0})  # dict\ndata.signals(MyDataclass())  # dataclass instance',
    )


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


class DatastarAttributes:
    """Generator for Datastar data-* attributes."""

    @overload
    def attr(self, key: str, expression: str) -> dict[str, str]: ...
    @overload
    def attr(self, key: dict[str, str]) -> dict[str, str]: ...
    def attr(self, key: str | dict[str, str], expression: str | None = None) -> dict[str, str]:
        if isinstance(key, str):
            return {f"data-attr:{key}": expression or ""}
        return {"data-attr": js(key)}

    def bind(self, signal_name: str) -> dict[str, str]:
        return {"data-bind": signal_name}

    @overload
    def class_(self, name: str, expression: str) -> dict[str, str]: ...
    @overload
    def class_(self, name: dict[str, str]) -> dict[str, str]: ...
    def class_(self, name: str | dict[str, str], expression: str | None = None) -> dict[str, str]:
        if isinstance(name, str):
            return {f"data-class:{name}": expression or ""}
        return {"data-class": js(name)}

    @overload
    def computed(self, key: str, expression: str) -> dict[str, str]: ...
    @overload
    def computed(self, key: dict[str, str]) -> dict[str, str]: ...
    def computed(self, key: str | dict[str, str], expression: str | None = None) -> dict[str, str]:
        if isinstance(key, str):
            kebab_key, from_case = to_kebab_key(key)
            if from_case == "camel":
                return {f"data-computed:{kebab_key}": expression or ""}
            return {f"data-computed:{kebab_key}__case.{from_case}": expression or ""}
        kebab_cases = [
            (to_kebab_key(k), value) for k, value in key.items()
        ]
        return {
            (
                f"data-computed:{kebab_key}"
                if from_case == "camel"
                else f"data-computed:{kebab_key}__case.{from_case}"
            ): value
            for (kebab_key, from_case), value in kebab_cases
        }

    def effect(self, expression: str) -> dict[str, str]:
        return {"data-effect": expression}

    def ignore(self, self_only: bool = False) -> dict[str, bool]:
        return {"data-ignore__self": True} if self_only else {"data-ignore": True}

    def ignore_morph(self) -> dict[str, bool]:
        return {"data-ignore-morph": True}

    def indicator(self, signal_name: str) -> dict[str, str]:
        return {"data-indicator": signal_name}

    def init(
        self,
        expression: str,
        *,
        delay: TimeValue | None = None,
        viewtransition: bool = False,
    ) -> dict[str, str]:
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
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        terse: bool = False,
    ) -> dict[str, str | bool]:
        if include is not None or exclude is not None:
            d: dict[str, str] = {}
            if include is not None:
                d["include"] = s(parse_filter_value(include))
            if exclude is not None:
                d["exclude"] = s(parse_filter_value(exclude))
            value: str | bool = js(d)
        else:
            value = True

        return (
            {"data-json-signals__terse": value}
            if terse
            else {"data-json-signals": value}
        )

    def on_intersect(
        self,
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
        self,
        expression: str,
        *,
        duration: TimeValue | tuple[TimeValue, Literal["leading"]] = "1s",
        viewtransition: bool = False,
    ) -> dict[str, str]:
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
        self,
        expression: str,
        *,
        delay: TimeValue | None = None,
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> dict[str, str]:
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
        self,
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

    def preserve_attr(self, attrs: str | list[str]) -> dict[str, str]:
        value = attrs if isinstance(attrs, str) else " ".join(attrs)
        return {"data-preserve-attr": value}

    def ref(self, signal_name: str) -> dict[str, str]:
        return {"data-ref": signal_name}

    def show(self, expression: str) -> dict[str, str]:
        return {"data-show": expression}

    @overload
    def signals(self, key: str, expression: str, *, ifmissing: bool = ...) -> dict[str, str]: ...
    @overload
    def signals(self, key: dict[str, SignalValue] | Any, *, ifmissing: bool = ...) -> dict[str, str]: ...
    def signals(
        self,
        key: str | dict[str, SignalValue] | Any,
        expression: str | None = None,
        *,
        ifmissing: bool = False,
    ) -> dict[str, str]:
        """
        Generate data-signals attribute.

        Single key-value (expression is a Datastar/JS expression):
            data.signals("count", "0")
            data.signals("name", s("hello"))
            data.signals("count", "0", ifmissing=True)

        Multiple signals (values are Python values, JSON-encoded):
            data.signals({"count": 0, "name": ""})

        Also accepts dataclass instances, Pydantic models, etc.:
            data.signals(FormState())
        """
        if isinstance(key, str):
            kebab_key, from_case = to_kebab_key(key)
            mods = ""
            if from_case != "camel":
                mods += f"__case.{from_case}"
            if ifmissing:
                mods += "__ifmissing"
            return {f"data-signals:{kebab_key}{mods}": expression or ""}
        signals_dict = _to_dict(key)
        attr_key = "data-signals__ifmissing" if ifmissing else "data-signals"
        return {
            attr_key: json.dumps(signals_dict, separators=(",", ":"), ensure_ascii=False)
        }

    @overload
    def style(self, prop: str, expression: str) -> dict[str, str]: ...
    @overload
    def style(self, prop: dict[str, str]) -> dict[str, str]: ...
    def style(self, prop: str | dict[str, str], expression: str | None = None) -> dict[str, str]:
        if isinstance(prop, str):
            return {f"data-style:{prop}": expression or ""}
        return {"data-style": js(prop)}

    def text(self, expression: str) -> dict[str, str]:
        return {"data-text": expression}
