"""Datastar HTML attribute helpers.

Use the exported `data` instance:

```python
from stario.datastar import at, data

h.Button(data.on("click", at.post("/cart")), "Add")
```

Each method returns a pre-rendered `Attrs` fragment (opening-tag attribute bytes).
The default instance emits normal `data-*` attributes. Create another instance when serving a
custom Datastar bundle with an aliased prefix:

```python
from stario.datastar import DatastarAttributes

star = DatastarAttributes("data-star-")
h.Div(star.text("$title"))
```

Reference: https://data-star.dev/reference/attributes
"""

import json
from collections.abc import Mapping
from typing import Literal

from stario.exceptions import StarioError
from stario.markup.escape import escape_attribute_value as escape_attr
from stario.markup.escape import escape_sq_attribute_value
from stario.markup.types import Attrs

from ._jsevents import JSEvent
from .format import (
    Case,
    Debounce,
    FilterValue,
    SignalValue,
    Throttle,
    TimeValue,
    debounce_to_string,
    filter_js,
    js_object,
    require_mapping,
    signal_key,
    signal_path_key,
    throttle_to_string,
    time_to_string,
    to_kebab_key,
    validate_signal_path,
)


class DatastarAttributes:
    """Namespace for Datastar `data-*` attributes.

    Each helper returns an `Attrs` fragment (pre-rendered opening-tag bytes).
    JavaScript expressions and `js_object()` / `filter_js()` output are
    trusted Datastar content. Helpers escape them for HTML attribute boundaries,
    but do not parse or sanitize the JavaScript itself. `signals()` wraps JSON
    in single-quoted attributes and escapes only `'`, `&`, and `<`/`>` —
    so JSON double quotes stay literal on the wire.

    `prefix` defaults to `"data-"`. Pass an explicit Datastar alias prefix, such
    as `"data-star-"`, when loading a custom aliased Datastar bundle.
    """

    __slots__ = ("prefix",)

    def __init__(self, prefix: str = "data-") -> None:
        if not prefix:
            raise ValueError("Datastar attribute prefix cannot be empty.")
        self.prefix = prefix if prefix.endswith("-") else prefix + "-"

    def attr(self, key: str, expression: str) -> Attrs:
        """Set one HTML attribute from a reactive expression.

        ```python
        data.attr("title", "$item.label")
        # Attrs(' data-attr:title="$item.label"')
        ```
        """
        return Attrs(f' {self.prefix}attr:{key}="{escape_attr(expression)}"')

    def attrs(self, mapping: dict[str, str]) -> Attrs:
        """Set several HTML attributes from a mapping of expressions.

        ```python
        data.attrs({"open": "sidebarOpen"})
        # Attrs(' data-attr="{'open':sidebarOpen}"')
        ```
        """
        return Attrs(f' {self.prefix}attr="{escape_attr(js_object(mapping))}"')

    def bind(
        self,
        signal_name: str,
        *,
        prop: str | None = None,
        event: str | None = None,
    ) -> Attrs:
        """Two-way bind an element value to a signal.

        ```python
        data.bind("email")
        # Attrs(' data-bind="email"')

        data.bind("is_checked", prop="checked", event="change")
        # Attrs(' data-bind:is-checked__case.snake__prop.checked__event.change="is_checked"')
        ```
        """
        validate_signal_path(signal_name)
        if prop is None and event is None:
            return Attrs(f' {self.prefix}bind="{escape_attr(signal_name)}"')

        key_suffix = signal_path_key(signal_name)
        if prop is None:
            return Attrs(
                f' {self.prefix}bind:{key_suffix}__event.{event}="'
                f'{escape_attr(signal_name)}"'
            )
        if event is None:
            return Attrs(
                f' {self.prefix}bind:{key_suffix}__prop.{prop}="'
                f'{escape_attr(signal_name)}"'
            )
        return Attrs(
            f' {self.prefix}bind:{key_suffix}__prop.{prop}__event.{event}="'
            f'{escape_attr(signal_name)}"'
        )

    def class_(self, name: str, expression: str) -> Attrs:
        """Toggle one CSS class from a reactive expression.

        ```python
        data.class_("hidden", "!$expanded")
        # Attrs(' data-class:hidden="!$expanded"')
        ```
        """
        return Attrs(f' {self.prefix}class:{name}="{escape_attr(expression)}"')

    def classes(self, mapping: dict[str, str]) -> Attrs:
        """Toggle several CSS classes from a mapping of expressions.

        ```python
        data.classes({"loading": "$pending"})
        # Attrs(' data-class="{'loading':$pending}"')
        ```
        """
        return Attrs(f' {self.prefix}class="{escape_attr(js_object(mapping))}"')

    def computed(self, key: str, expression: str) -> Attrs:
        """Create one computed signal from a reactive expression.

        ```python
        data.computed("full_name", "$first + $last")
        # Attrs(' data-computed:full-name__case.snake="$first + $last"')
        ```
        """
        return Attrs(
            f' {self.prefix}computed:{signal_key(key)}="{escape_attr(expression)}"'
        )

    def computeds(self, mapping: dict[str, str]) -> Attrs:
        """Create several computed signals from expressions.

        ```python
        data.computeds({"full_name": "$a", "initials": "$b"})
        # Attrs(' data-computed:full-name__case.snake="$a" data-computed:initials__case.snake="$b"')
        ```
        """
        prefix = self.prefix + "computed:"
        return Attrs(
            "".join(
                f' {prefix}{signal_key(key)}="{escape_attr(value)}"'
                for key, value in mapping.items()
            )
        )

    def effect(self, expression: str) -> Attrs:
        """Run a side effect when the element initializes or updates.

        ```python
        data.effect("el.focus()")
        # Attrs(' data-effect="el.focus()"')
        ```
        """
        return Attrs(f' {self.prefix}effect="{escape_attr(expression)}"')

    def ignore(self, self_only: bool = False) -> Attrs:
        """Skip Datastar processing for this element or subtree.

        ```python
        data.ignore()
        # Attrs(' data-ignore')

        data.ignore(self_only=True)
        # Attrs(' data-ignore__self')
        ```
        """
        if self_only:
            return Attrs(f" {self.prefix}ignore__self")
        return Attrs(f" {self.prefix}ignore")

    def ignore_morph(self) -> Attrs:
        """Prevent backend patches from morphing this subtree.

        ```python
        data.ignore_morph()
        # Attrs(' data-ignore-morph')
        ```
        """
        return Attrs(f" {self.prefix}ignore-morph")

    def indicator(self, signal_name: str) -> Attrs:
        """Track in-flight fetch state in a signal.

        ```python
        data.indicator("saving")
        # Attrs(' data-indicator="saving"')
        ```
        """
        validate_signal_path(signal_name)
        return Attrs(f' {self.prefix}indicator="{escape_attr(signal_name)}"')

    def init(
        self,
        expression: str,
        *,
        delay: TimeValue | None = None,
        view_transition: bool = False,
    ) -> Attrs:
        """Run an expression on element initialization.

        ```python
        data.init("setup()")
        # Attrs(' data-init="setup()"')

        data.init("setup()", delay="200ms", view_transition=True)
        # Attrs(' data-init__delay.200ms__viewtransition="setup()"')
        ```
        """
        if delay is None:
            if view_transition:
                return Attrs(
                    f' {self.prefix}init__viewtransition="{escape_attr(expression)}"'
                )
            return Attrs(f' {self.prefix}init="{escape_attr(expression)}"')

        modifiers = "delay." + time_to_string(delay)
        if view_transition:
            modifiers += "__viewtransition"
        return Attrs(f' {self.prefix}init__{modifiers}="{escape_attr(expression)}"')

    def json_signals(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        terse: bool = False,
    ) -> Attrs:
        """Render signals as JSON text.

        ```python
        data.json_signals()
        # Attrs(' data-json-signals')

        data.json_signals(include=["email", "password"], terse=True)
        # Attrs(' data-json-signals__terse="{'include':'email|password'}"')
        ```
        """
        filters = filter_js(include, exclude)
        value: str | bool = filters if filters is not None else True
        if terse:
            key = self.prefix + "json-signals__terse"
        else:
            key = self.prefix + "json-signals"
        if value is True:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(value)}"')

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
        view_transition: bool = False,
        target: Literal["window", "document", "outside"] | None = None,
        prevent: bool = False,
        stop: bool = False,
        case: Case = "kebab",
    ) -> Attrs:
        """Listen for an event.

        ```python
        data.on("click", "@get('/cart/count')")
        # Attrs(' data-on:click="@get('/cart/count')"')

        data.on("click", "$open = false", target="outside")
        # Attrs(' data-on:click__outside="$open = false"')
        ```
        """
        if passive and prevent:
            raise StarioError(
                "passive and prevent contradict each other",
                context={"event": event},
                help_text=(
                    "`passive` promises the listener never calls preventDefault; "
                    "`prevent` calls it. Pass only one."
                ),
            )

        if (
            not once
            and not passive
            and not capture
            and delay is None
            and debounce is None
            and throttle is None
            and not view_transition
            and target is None
            and not prevent
            and not stop
            and case == "kebab"
            and event.islower()
            and "_" not in event
        ):
            return Attrs(f' {self.prefix}on:{event}="{escape_attr(expression)}"')

        modifiers: list[str] = []
        if once:
            modifiers.append("once")
        if passive:
            modifiers.append("passive")
        if capture:
            modifiers.append("capture")
        if target is not None:
            modifiers.append(target)
        if prevent:
            modifiers.append("prevent")
        if stop:
            modifiers.append("stop")
        if delay is not None:
            modifiers.append("delay." + time_to_string(delay))
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))
        if view_transition:
            modifiers.append("viewtransition")

        kebab_event = to_kebab_key(event)
        if case != "kebab":
            modifiers.append("case." + case)

        if modifiers:
            return Attrs(
                f' {self.prefix}on:{kebab_event}__{"__".join(modifiers)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on:{kebab_event}="{escape_attr(expression)}"')

    def on_intersect(
        self,
        expression: str,
        *,
        threshold: float | Literal["half", "full"] | str | None = None,
        once: bool = False,
        exit: bool = False,
        delay: TimeValue | None = None,
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
    ) -> Attrs:
        """React to viewport intersection.

        ```python
        data.on_intersect("load()", threshold=0.25, once=True)
        # Attrs(' data-on-intersect__threshold.25__once="load()"')
        ```
        """
        modifiers: list[str] = []
        if threshold is not None:
            if isinstance(threshold, str):
                modifiers.append(
                    threshold
                    if threshold in ("half", "full")
                    else "threshold." + threshold
                )
            elif 0.0 <= threshold <= 1.0:
                modifiers.append(f"threshold.{round(threshold * 100)}")
            else:
                raise StarioError(
                    f"Invalid intersection threshold: {threshold}",
                    context={
                        "threshold_value": str(threshold),
                        "threshold_type": type(threshold).__name__,
                    },
                    help_text=(
                        "Numeric thresholds are the visible fraction of the element and "
                        "must be between 0.0 and 1.0, or the strings 'half' / 'full'."
                    ),
                )
        if once:
            modifiers.append("once")
        if exit:
            modifiers.append("exit")
        if delay is not None:
            modifiers.append("delay." + time_to_string(delay))
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))

        if modifiers:
            return Attrs(
                f' {self.prefix}on-intersect__{"__".join(modifiers)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on-intersect="{escape_attr(expression)}"')

    def on_interval(
        self,
        expression: str,
        *,
        duration: TimeValue = "1s",
        leading: bool = False,
        view_transition: bool = False,
    ) -> Attrs:
        """Run an expression on an interval.

        ```python
        data.on_interval("tick()")
        # Attrs(' data-on-interval="tick()"')

        data.on_interval("tick()", duration="2s", leading=True)
        # Attrs(' data-on-interval__duration.2s.leading="tick()"')
        ```
        """
        if duration == "1s" and not leading:
            if view_transition:
                return Attrs(
                    f' {self.prefix}on-interval__viewtransition="'
                    f'{escape_attr(expression)}"'
                )
            return Attrs(f' {self.prefix}on-interval="{escape_attr(expression)}"')

        modifiers = "duration." + time_to_string(duration)
        if leading:
            modifiers += ".leading"
        if view_transition:
            modifiers += "__viewtransition"
        return Attrs(
            f' {self.prefix}on-interval__{modifiers}="{escape_attr(expression)}"'
        )

    def on_signal_patch(
        self,
        expression: str,
        *,
        delay: TimeValue | None = None,
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> Attrs:
        """React to signal patches.

        ```python
        data.on_signal_patch("save()", debounce="500ms", include=["draft"])
        # Attrs(' data-on-signal-patch__debounce.500ms="save()" data-on-signal-patch-filter="{'include':'draft'}"')
        ```
        """
        modifiers: list[str] = []
        if delay is not None:
            modifiers.append("delay." + time_to_string(delay))
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))

        if modifiers:
            key = f"{self.prefix}on-signal-patch__{'__'.join(modifiers)}"
        else:
            key = self.prefix + "on-signal-patch"

        filters = filter_js(include, exclude)
        if filters is not None:
            return Attrs(
                f' {key}="{escape_attr(expression)}"'
                f' {self.prefix}on-signal-patch-filter="'
                f'{escape_attr(filters)}"'
            )
        return Attrs(f' {key}="{escape_attr(expression)}"')

    def preserve_attr(self, attrs: str | list[str]) -> Attrs:
        """Preserve selected attributes during DOM morphing.

        ```python
        data.preserve_attr(["data-testid", "id"])
        # Attrs(' data-preserve-attr="data-testid id"')
        ```
        """
        value = attrs if isinstance(attrs, str) else " ".join(attrs)
        return Attrs(f' {self.prefix}preserve-attr="{escape_attr(value)}"')

    def ref(self, signal_name: str) -> Attrs:
        """Store the current element in a signal.

        ```python
        data.ref("search_input")
        # Attrs(' data-ref="search_input"')
        ```
        """
        validate_signal_path(signal_name)
        return Attrs(f' {self.prefix}ref="{escape_attr(signal_name)}"')

    def show(self, expression: str) -> Attrs:
        """Show or hide an element from a boolean expression.

        ```python
        data.show("$error != null")
        # Attrs(' data-show="$error != null"')
        ```
        """
        return Attrs(f' {self.prefix}show="{escape_attr(expression)}"')

    def signal(
        self,
        name: str,
        expression: str,
        *,
        if_missing: bool = False,
    ) -> Attrs:
        """Patch one signal from a Datastar expression.

        ```python
        data.signal("my_count", "0", if_missing=True)
        # Attrs(' data-signals:my-count__case.snake__ifmissing="0"')
        ```
        """
        if if_missing:
            return Attrs(
                f' {self.prefix}signals:{signal_key(name)}__ifmissing="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(
            f' {self.prefix}signals:{signal_key(name)}="{escape_attr(expression)}"'
        )

    def signals(
        self,
        payload: Mapping[str, SignalValue],
        *,
        if_missing: bool = False,
    ) -> Attrs:
        """Patch several signals.

        ```python
        data.signals({"count": 0, "open": False})
        # Attrs(" data-signals='{\"count\":0,\"open\":false}'")
        ```
        """
        value = escape_sq_attribute_value(
            json.dumps(
                dict(require_mapping("signals", payload)),
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        if if_missing:
            return Attrs(f" {self.prefix}signals__ifmissing='{value}'")
        return Attrs(f" {self.prefix}signals='{value}'")

    def style(self, prop: str, expression: str) -> Attrs:
        """Set one inline style property from a reactive expression.

        ```python
        data.style("width", "$pct + '%'")
        # Attrs(' data-style:width="$pct + '%'"')
        ```
        """
        return Attrs(f' {self.prefix}style:{prop}="{escape_attr(expression)}"')

    def styles(self, mapping: dict[str, str]) -> Attrs:
        """Set several inline style properties from expressions.

        ```python
        data.styles({"opacity": "$visible ? '1' : '0'"})
        # Attrs(' data-style="{'opacity':$visible ? '1' : '0'}"')
        ```
        """
        return Attrs(f' {self.prefix}style="{escape_attr(js_object(mapping))}"')

    def text(self, expression: str) -> Attrs:
        """Bind text content to a Datastar expression.

        ```python
        data.text("$greeting")
        # Attrs(' data-text="$greeting"')
        ```
        """
        return Attrs(f' {self.prefix}text="{escape_attr(expression)}"')

    def animate(self, expression: str) -> Attrs:
        """Animate element attributes over time. **Datastar Pro only.**

        ```python
        data.animate("$x")
        # Attrs(' data-animate="$x"')
        ```
        """
        return Attrs(f' {self.prefix}animate="{escape_attr(expression)}"')

    def custom_validity(self, expression: str) -> Attrs:
        """Set a custom validity message. **Datastar Pro only.**

        ```python
        data.custom_validity("$msg")
        # Attrs(' data-custom-validity="$msg"')
        ```
        """
        return Attrs(f' {self.prefix}custom-validity="{escape_attr(expression)}"')

    def match_media(self, signal_name: str, expression: str) -> Attrs:
        """Sync a signal with `matchMedia`. **Datastar Pro only.**

        ```python
        data.match_media("is_dark", "'prefers-color-scheme: dark'")
        # Attrs(' data-match-media:is-dark__case.snake="'prefers-color-scheme: dark'"')
        ```
        """
        return Attrs(
            f' {self.prefix}match-media:{signal_key(signal_name)}="'
            f'{escape_attr(expression)}"'
        )

    def on_raf(
        self,
        expression: str,
        *,
        throttle: Throttle | None = None,
    ) -> Attrs:
        """Run an expression on every animation frame. **Datastar Pro only.**

        ```python
        data.on_raf("draw()", throttle="100ms")
        # Attrs(' data-on-raf__throttle.100ms="draw()"')
        ```
        """
        if throttle is not None:
            return Attrs(
                f' {self.prefix}on-raf__{throttle_to_string(throttle)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on-raf="{escape_attr(expression)}"')

    def on_resize(
        self,
        expression: str,
        *,
        debounce: Debounce | None = None,
        throttle: Throttle | None = None,
    ) -> Attrs:
        """React to element resize. **Datastar Pro only.**

        ```python
        data.on_resize("layout()", debounce="50ms", throttle="100ms")
        # Attrs(' data-on-resize__debounce.50ms__throttle.100ms="layout()"')
        ```
        """
        modifiers: list[str] = []
        if debounce is not None:
            modifiers.append(debounce_to_string(debounce))
        if throttle is not None:
            modifiers.append(throttle_to_string(throttle))

        if modifiers:
            return Attrs(
                f' {self.prefix}on-resize__{"__".join(modifiers)}="'
                f'{escape_attr(expression)}"'
            )
        return Attrs(f' {self.prefix}on-resize="{escape_attr(expression)}"')

    def persist(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        storage_key: str | None = None,
        session: bool = False,
    ) -> Attrs:
        """Persist signals. **Datastar Pro only.**

        ```python
        data.persist(include="draft", storage_key="prefs", session=True)
        # Attrs(' data-persist:prefs__session="{'include':'draft'}"')
        ```
        """
        filters = filter_js(include, exclude)
        value: str | bool = filters if filters is not None else True
        if storage_key is None:
            key = self.prefix + "persist"
        elif session:
            key = f"{self.prefix}persist:{storage_key}__session"
        else:
            key = f"{self.prefix}persist:{storage_key}"
        if value is True:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(value)}"')

    def query_string(
        self,
        *,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        filter_empty: bool = False,
        history: bool = False,
    ) -> Attrs:
        """Sync signals with the URL. **Datastar Pro only.**

        ```python
        data.query_string(include="page", history=True)
        # Attrs(' data-query-string__history="{'include':'page'}"')
        ```
        """
        modifiers: list[str] = []
        if filter_empty:
            modifiers.append("filter")
        if history:
            modifiers.append("history")

        if modifiers:
            key = f"{self.prefix}query-string__{'__'.join(modifiers)}"
        else:
            key = self.prefix + "query-string"
        filters = filter_js(include, exclude)
        value: str | bool = filters if filters is not None else True
        if value is True:
            return Attrs(f" {key}")
        return Attrs(f' {key}="{escape_attr(value)}"')

    def replace_url(self, expression: str) -> Attrs:
        """Replace the current browser URL. **Datastar Pro only.**

        ```python
        data.replace_url("`/page/${$page}`")
        # Attrs(' data-replace-url="`/page/${$page}`"')
        ```
        """
        return Attrs(f' {self.prefix}replace-url="{escape_attr(expression)}"')

    def scroll_into_view(
        self,
        *,
        behavior: Literal["smooth", "instant", "auto"] | None = None,
        horizontal: Literal["start", "center", "end", "nearest"] | None = None,
        vertical: Literal["start", "center", "end", "nearest"] | None = None,
        focus: bool = False,
    ) -> Attrs:
        """Scroll this element into view. **Datastar Pro only.**

        ```python
        data.scroll_into_view(behavior="smooth", vertical="center", focus=True)
        # Attrs(' data-scroll-into-view__smooth__vcenter__focus')
        ```
        """
        modifiers: list[str] = []
        if behavior is not None:
            modifiers.append(behavior)
        if horizontal is not None:
            modifiers.append("h" + horizontal)
        if vertical is not None:
            modifiers.append("v" + vertical)
        if focus:
            modifiers.append("focus")

        if modifiers:
            return Attrs(f" {self.prefix}scroll-into-view__{'__'.join(modifiers)}")
        return Attrs(f" {self.prefix}scroll-into-view")

    def view_transition(self, expression: str) -> Attrs:
        """Set `view-transition-name`. **Datastar Pro only.**

        ```python
        data.view_transition("$id")
        # Attrs(' data-view-transition="$id"')
        ```
        """
        return Attrs(f' {self.prefix}view-transition="{escape_attr(expression)}"')


data = DatastarAttributes()
