import json
import re
from collections.abc import Iterable
from typing import Annotated, Literal

from stario.application import Stario
from stario.exceptions import DatastarConfigError

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


# Datastar Actions Literals
ContentType = Literal["json", "form"]
RequestCancellation = Literal["auto", "disabled"]

# Filter types for include/exclude parameters
FilterValue = str | Iterable[str]


type SignalValue = (
    str | int | float | bool | dict[str, SignalValue] | list[SignalValue] | None
)


type TimeValue = int | float | str
"""
We measure time in seconds.
int = number of whole seconds, adds "s" suffix. 10 => 10s.
float = possible fractional seconds, adds "ms" suffix. 0.5 => 500ms.
str = no parsing
"""


def time_to_string(time: TimeValue) -> str:
    """
    Convert a time value to a Datastar-compatible time string.

    Args:
        time: Time value as int (seconds), float (seconds), or string

    Returns:
        Formatted time string for Datastar attributes

    Examples:
        >>> time_to_string(5)
        '5s'
        >>> time_to_string(0.5)
        '500ms'
        >>> time_to_string("2s")
        '2s'
    """
    if isinstance(time, float):
        return f"{int(time * 1000)}ms"
    if isinstance(time, int):
        return f"{int(time)}s"
    return time


type Debounce = (
    TimeValue
    | tuple[TimeValue, Literal["leading", "notrailing"]]
    | tuple[
        TimeValue, Literal["leading", "notrailing"], Literal["leading", "notrailing"]
    ]
)

type Throttle = (
    TimeValue
    | tuple[TimeValue, Literal["noleading", "trailing"]]
    | tuple[
        TimeValue, Literal["noleading", "trailing"], Literal["noleading", "trailing"]
    ]
)


def debounce_to_string(debounce: Debounce) -> str:
    """
    Convert a debounce configuration to a Datastar modifier string.

    Debouncing delays the execution of an event handler until after a specified
    time has passed since the last event.

    Args:
        debounce: Time value or tuple with time and modifiers ('leading', 'notrailing')

    Returns:
        Datastar debounce modifier string

    Examples:
        >>> debounce_to_string(0.5)
        'debounce.500ms'
        >>> debounce_to_string((1, "leading"))
        'debounce.1s.leading'
        >>> debounce_to_string((0.3, "leading", "notrailing"))
        'debounce.300ms.leading.notrailing'
    """
    # shortcut for the most common case
    if isinstance(debounce, (int, float, str)):
        return "debounce." + time_to_string(debounce)

    # from typing know it's some tuple:
    if len(debounce) == 2:
        return f"debounce.{time_to_string(debounce[0])}.{debounce[1]}"

    # the last option would be a tuple of 3 elements
    if len(debounce) == 3:
        return f"debounce.{time_to_string(debounce[0])}.{debounce[1]}.{debounce[2]}"

    # or something else that we don't support
    raise DatastarConfigError(
        f"Invalid debounce configuration: {debounce}",
        context={
            "debounce_value": str(debounce),
            "debounce_type": type(debounce).__name__,
        },
        help_text="Debounce must be a time value (int/float/str) or a tuple with time and modifiers.",
        example="""from stario.datastar import Datastar

ds = Datastar()

# Simple debounce (time only):
ds.on("click", "action()", debounce=500)        # 500ms
ds.on("click", "action()", debounce="1s")       # 1 second
ds.on("click", "action()", debounce=1.5)        # 1500ms
# With single modifier:
ds.on("input", "action()", debounce=(300, "notrailing"))

# With two modifiers:
ds.on("input", "action()", debounce=(300, "notrailing", "noleading"))""",
    )


def throttle_to_string(throttle: Throttle) -> str:
    """
    Convert a throttle configuration to a Datastar modifier string.

    Throttling ensures an event handler executes at most once per specified time period.

    Args:
        throttle: Time value or tuple with time and modifiers ('noleading', 'trailing')

    Returns:
        Datastar throttle modifier string

    Examples:
        >>> throttle_to_string(1)
        'throttle.1s'
        >>> throttle_to_string((0.5, "trailing"))
        'throttle.500ms.trailing'
        >>> throttle_to_string((2, "noleading", "trailing"))
        'throttle.2s.noleading.trailing'
    """
    # shortcut for the most common case
    if isinstance(throttle, (int, float, str)):
        return "throttle." + time_to_string(throttle)

    # from typing know it's some tuple:
    if len(throttle) == 2:
        return f"throttle.{time_to_string(throttle[0])}.{throttle[1]}"

    # the last option would be a tuple of 3 elements
    if len(throttle) == 3:
        return f"throttle.{time_to_string(throttle[0])}.{throttle[1]}.{throttle[2]}"

    # or something else that we don't support
    raise DatastarConfigError(
        f"Invalid throttle configuration: {throttle}",
        context={
            "throttle_value": str(throttle),
            "throttle_type": type(throttle).__name__,
        },
        help_text="Throttle must be a time value (int/float/str) or a tuple with time and modifiers.",
        example="""from stario.datastar import Datastar

ds = Datastar()

# Simple throttle (time only):
ds.on("scroll", "action()", throttle=100)       # 100ms
ds.on("scroll", "action()", throttle="500ms")   # 500ms
ds.on("scroll", "action()", throttle=1.5)       # 1500ms

# With single modifier:
ds.on("scroll", "action()", throttle=(100, "trailing"))

# With two modifiers:
ds.on("scroll", "action()", throttle=(100, "noleading", "trailing"))""",
    )


def quick_js_dump(obj: dict) -> str:
    """
    Quick JavaScript object notation dump for small, possibly nested dictionaries used in Datastar attributes.

    This is an optimized serializer for key-value pairs commonly used in Datastar attributes.
    Returns JavaScript object notation with unquoted keys and string values as expressions.
    Supports string (as expressions), number, boolean, None, and nested dict values.

    Args:
        obj: Dictionary with string keys and values (str, int, float, bool, None, or dict)

    Returns:
        JavaScript object notation string representation

    Examples:
        >>> quick_js_dump({"title": "Hello", "disabled": "$isDisabled"})
        '{title:Hello,disabled:$isDisabled}'
        >>> quick_js_dump({"class": "active"})
        '{class:active}'
        >>> quick_js_dump({"count": 5, "flag": True})
        '{count:5,flag:true}'
        >>> quick_js_dump({"user": {"name": "Alice", "age": 30}})
        '{user:{name:Alice,age:30}}'
        >>> quick_js_dump({"outer": {"inner": {"foo": "bar"}}})
        '{outer:{inner:{foo:bar}}}'
        >>> quick_js_dump({"value": None})
        '{value:null}'
    """

    def _to_js_value(v):
        if isinstance(v, dict):
            return quick_js_dump(v)
        elif isinstance(v, str):
            # Treat strings as expressions (output without quotes)
            return v
        elif v is None:
            return "null"
        elif isinstance(v, bool):
            return "true" if v else "false"
        elif isinstance(v, (int, float)):
            return str(v)
        else:
            raise TypeError(
                f"quick_json_dump only supports str, bool, int, float, None, dict values, got {type(v).__name__}"
            )

    items = [f"{k}:{_to_js_value(v)}" for k, v in obj.items()]
    return "{" + ",".join(items) + "}"


def parse_filter_value(value: FilterValue) -> str:
    """
    Parse a filter value for include/exclude parameters in Datastar actions.

    Converts filter specifications into regex patterns for signal filtering.
    String values are used as-is (assumed to be regex), while iterables
    are converted to exact-match regex patterns.

    Args:
        value: Either a string (used as-is) or an iterable of strings
                (parsed as regex to match exactly those strings)

    Returns:
        A regex string that matches the filter criteria

    Examples:
        >>> parse_filter_value("foo.*")
        'foo.*'
        >>> parse_filter_value(["foo", "bar"])
        'foo|bar'
        >>> parse_filter_value(("user", "admin"))
        'user|admin'
        >>> parse_filter_value({"count", "total"}) in ('count|total', 'total|count')
        True
        >>> parse_filter_value(iter(["signal1", "signal2"]))
        'signal1|signal2'
    """
    if isinstance(value, str):
        return value

    # Handle any iterable (list, tuple, set, generator, etc.)
    # Escape special regex characters and join with |
    escaped_items = [re.escape(str(item)) for item in value]
    return "|".join(escaped_items)


type Case = Literal["kebab", "snake", "pascal", "camel"]


def to_kebab_key(key: str) -> tuple[str, Case]:
    """
    Convert a key to kebab-case and detect its original casing style.

    Datastar uses kebab-case for attribute names, but supports multiple
    casing styles via the __case modifier. This function converts keys
    to kebab-case and identifies the original case for proper modifier generation.

    Args:
        key: String in any casing style (camelCase, PascalCase, snake_case, kebab-case)

    Returns:
        Tuple of (kebab-case string, original case style)

    Examples:
        >>> to_kebab_key("mySignal")
        ('my-signal', 'camel')
        >>> to_kebab_key("MySignal")
        ('my-signal', 'pascal')
        >>> to_kebab_key("my_signal")
        ('my-signal', 'snake')
        >>> to_kebab_key("my-signal")
        ('my-signal', 'kebab')
        >>> to_kebab_key("signal")
        ('signal', 'kebab')
    """
    # snake_case (contains '_') - check before lowercase check
    if "_" in key:
        return key.replace("_", "-").lower(), "snake"

    # kebab-case (contains '-')
    if "-" in key:
        return key.lower(), "kebab"

    # all lowercase (already kebab)
    if key.islower():
        return key, "kebab"

    # PascalCase (first char uppercase)
    # Insert dash before every uppercase except first, then lowercase
    if key[0].isupper():
        return (
            "".join(
                (
                    ("-" if i != 0 and c.isupper() else "") + c.lower()
                    for i, c in enumerate(key)
                )
            ),
            "pascal",
        )

    # camelCase (first char lowercase, has uppercase somewhere)
    # Insert dash before every uppercase, then lowercase
    return (
        "".join((("-" + c.lower()) if c.isupper() else c for c in key)),
        "camel",
    )


class DatastarAttributes:
    """
    Generator for Datastar data-* attributes.

    This class provides methods to generate Datastar HTML attributes with proper
    formatting and type safety. These attributes don't require any external
    dependencies.

    Reference: https://data-star.dev/reference/attributes

    Examples:
        >>> from stario.datastar import DatastarAttributes
        >>> attrs = DatastarAttributes()
        >>> attrs.text("$message")
        {'data-text': '$message'}
        >>> attrs.show("$isVisible")
        {'data-show': '$isVisible'}
    """

    def attr(self, attr_dict: dict[str, str]) -> dict[str, str]:
        """
        Set the value of HTML attributes to expressions and keep them in sync.

        The data-attr attribute dynamically updates HTML attribute values based on
        signal expressions. When signals change, the attributes automatically update.

        Reference: https://data-star.dev/reference/attributes#data-attr

        Args:
            attr_dict: Dictionary mapping attribute names to Datastar expressions

        Returns:
            Dictionary with data-attr attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.attr({"title": "$tooltip", "disabled": "$isDisabled"})
            {'data-attr': '{"title":"$tooltip","disabled":"$isDisabled"}'}
        """
        return {"data-attr": quick_js_dump(attr_dict)}

    def bind(self, signal_name: str) -> dict[str, str]:
        """
        Set up two-way data binding between a signal and an element's value.

        Creates a signal (if it doesn't exist) and synchronizes it with the element's
        value. Updates flow both ways: signal changes update the element, and element
        changes (via user input) update the signal.

        Reference: https://data-star.dev/reference/attributes#data-bind

        Args:
            signal_name: Name of the signal to bind to

        Returns:
            Dictionary with data-bind attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.bind("username")
            {'data-bind': 'username'}
            >>> attrs.bind("email")
            {'data-bind': 'email'}
        """
        return {"data-bind": signal_name}

    def class_(self, class_dict: dict[str, str]) -> dict[str, str]:
        """
        Add or remove CSS classes based on expressions.

        Each key-value pair represents a class name and a boolean expression.
        When the expression evaluates to true, the class is added; otherwise removed.

        Reference: https://data-star.dev/reference/attributes#data-class

        Args:
            class_dict: Dictionary mapping class names to boolean expressions

        Returns:
            Dictionary with data-class attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.class_({"hidden": "$isHidden", "active": "$isActive"})
            {'data-class': '{"hidden":"$isHidden","active":"$isActive"}'}
        """
        return {"data-class": quick_js_dump(class_dict)}

    def computed(self, computed_dict: dict[str, str]) -> dict[str, str]:
        """
        Create read-only computed signals based on expressions.

        Computed signals automatically recalculate when their dependencies change.
        They're useful for memoizing derived values and avoiding redundant calculations.
        Signal names are automatically converted to camelCase.

        Reference: https://data-star.dev/reference/attributes#data-computed

        Args:
            computed_dict: Dictionary mapping signal names to expressions

        Returns:
            Dictionary with data-computed-* attributes with proper case modifiers

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.computed({"fullName": "$firstName + ' ' + $lastName"})
            {'data-computed:full-name': "$firstName + ' ' + $lastName"}
            >>> attrs.computed({"total_price": "$quantity * $price"})
            {'data-computed:total-price__case.snake': '$quantity * $price'}
        """

        # 2-pass: first collect kebab/from_case, then build dict
        kebab_cases = [
            (to_kebab_key(key), value) for key, value in computed_dict.items()
        ]
        result = {
            (
                f"data-computed:{kebab_key}"
                if from_case == "camel"  # Default
                else f"data-computed:{kebab_key}__case.{from_case}"
            ): value
            for (kebab_key, from_case), value in kebab_cases
        }

        return result

    def effect(self, expression: str) -> dict[str, str]:
        """
        Execute an expression on load and whenever dependencies change.

        Effects run immediately on page load and re-run whenever any signals
        referenced in the expression change. Use for side effects like updating
        other signals, making backend requests, or DOM manipulation.

        Reference: https://data-star.dev/reference/attributes#data-effect

        Args:
            expression: Datastar expression to execute

        Returns:
            Dictionary with data-effect attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.effect("$total = $price * $quantity")
            {'data-effect': '$total = $price * $quantity'}
            >>> attrs.effect("console.log($count)")
            {'data-effect': 'console.log($count)'}
        """
        return {"data-effect": expression}

    def ignore(self, self_only: bool = False) -> dict[str, bool]:
        """
        Tell Datastar to ignore an element and optionally its descendants.

        Prevents Datastar from processing data-* attributes on the element.
        Useful for avoiding conflicts with third-party libraries or when
        displaying user-generated content.

        Reference: https://data-star.dev/reference/attributes#data-ignore

        Args:
            self_only: If True, only ignore the element itself, not its children

        Returns:
            Dictionary with data-ignore or data-ignore__self attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.ignore()
            {'data-ignore': True}
            >>> attrs.ignore(self_only=True)
            {'data-ignore__self': True}
        """
        if self_only:
            return {"data-ignore__self": True}
        return {"data-ignore": True}

    def ignore_morph(self) -> dict[str, bool]:
        """
        Tell Datastar to skip morphing this element during DOM updates.

        Prevents the element and its children from being updated when the
        PatchElements watcher processes DOM morphing operations.

        Reference: https://data-star.dev/reference/attributes#data-ignore-morph

        Returns:
            Dictionary with data-ignore-morph attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.ignore_morph()
            {'data-ignore-morph': True}
        """
        return {"data-ignore-morph": True}

    def indicator(self, signal_name: str) -> dict[str, str]:
        """
        Create a boolean signal that tracks fetch request status.

        The signal is automatically set to true when a fetch request starts
        and false when it completes. Useful for showing loading indicators.

        Reference: https://data-star.dev/reference/attributes#data-indicator

        Args:
            signal_name: Name of the indicator signal to create

        Returns:
            Dictionary with data-indicator attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.indicator("loading")
            {'data-indicator': 'loading'}
            >>> attrs.indicator("isFetching")
            {'data-indicator': 'isFetching'}
        """
        return {"data-indicator": signal_name}

    def init(
        self,
        expression: str,
        *,
        delay: TimeValue | None = None,
        viewtransition: bool = False,
    ) -> dict[str, str]:
        """
        Execute an expression when the element loads.

        Triggers once when the element is first added to the DOM and
        Datastar initializes it. Useful for lazy-loading data.

        Reference: https://data-star.dev/reference/attributes#data-on-load

        Args:
            expression: Datastar expression to execute
            delay: Delay before executing expression
            viewtransition: Use view transitions for updates

        Returns:
            Dictionary with data-on-load attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.init("$init()")
            {'data-init': '$init()'}
            >>> attrs.init("$fetchData()", delay=1)
            {'data-init__delay.1s': '$fetchData()'}
        """
        if delay is None:
            if not viewtransition:
                return {"data-init": expression}  # default

            return {"data-init__viewtransition": expression}

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
        """
        Create an element containing JSON representation of signals.

        Serializes signals to JSON within the element's content. Useful for
        passing signal state to JavaScript or debugging. Can filter signals
        using regex patterns.

        Reference: https://data-star.dev/reference/attributes#data-json-signals

        Args:
            include: Regex pattern or collection to match signal names to include
            exclude: Regex pattern or collection to match signal names to exclude
            terse: If True, minify the JSON output

        Returns:
            Dictionary with data-json-signals attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.json_signals()
            {'data-json-signals': True}
            >>> attrs.json_signals(include="user.*")
            {'data-json-signals': '{"include":"user.*"}'}
            >>> attrs.json_signals(exclude=["password", "token"], terse=True)
            {'data-json-signals__terse': '{"exclude":"password|token"}'}
        """

        # Fastest possible: minimize branches, dict creation, and function calls.
        # Use local vars, avoid repeated lookups, and combine logic.
        if include is not None or exclude is not None:
            d = {}
            if include is not None:
                d["include"] = parse_filter_value(include)
            if exclude is not None:
                d["exclude"] = parse_filter_value(exclude)
            value = quick_js_dump(d)
        else:
            value = True

        if terse:
            return {"data-json-signals__terse": value}
        return {"data-json-signals": value}

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
        """
        Execute an expression when an element intersects with the viewport.

        Triggers when the element becomes visible (intersects with viewport).
        Uses the Intersection Observer API under the hood.

        Reference: https://data-star.dev/reference/attributes#data-on-intersect

        Args:
            expression: Datastar expression to execute
            once: Fire only once, then remove the listener
            half: Trigger when at least 50% is visible
            full: Trigger when 100% is visible
            delay: Delay before executing expression
            debounce: Debounce configuration
            throttle: Throttle configuration
            viewtransition: Use view transitions for updates

        Returns:
            Dictionary with data-on-intersect attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.on_intersect("console.log('visible')")
            {'data-on-intersect': "console.log('visible')"}
            >>> attrs.on_intersect("$load()", once=True, half=True)
            {'data-on-intersect__once__half': '$load()'}
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

        if len(modifiers) > 0:
            mods = "__".join(modifiers)
            return {"data-on-intersect__" + mods: expression}

        return {"data-on-intersect": expression}

    def on_interval(
        self,
        expression: str,
        *,
        duration: TimeValue | tuple[TimeValue, Literal["leading"]] = "1s",
        viewtransition: bool = False,
    ) -> dict[str, str]:
        """
        Execute an expression at regular intervals.

        Runs the expression repeatedly at the specified interval. By default,
        waits for the first interval before executing (trailing edge). Use
        'leading' modifier to execute immediately on page load.

        Reference: https://data-star.dev/reference/attributes#data-on-interval

        Args:
            expression: Datastar expression to execute
            duration: Interval duration or tuple with duration and "leading" modifier
            viewtransition: Use view transitions for updates

        Returns:
            Dictionary with data-on-interval attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.on_interval("$refresh()")
            {'data-on-interval': '$refresh()'}
            >>> attrs.on_interval("$poll()", duration=5)
            {'data-on-interval__duration.5s': '$poll()'}
            >>> attrs.on_interval("$check()", duration=(2, "leading"))
            {'data-on-interval__duration.2s.leading': '$check()'}
        """
        if duration == "1s":
            if not viewtransition:
                return {"data-on-interval": expression}  # default

            return {"data-on-interval__viewtransition": expression}

        # TimeValue is a type alias (int | float | str), so we check for those types explicitly:
        if isinstance(duration, (int, float, str)):
            mods = "duration." + time_to_string(duration)

        elif isinstance(duration, tuple):
            mods = f"duration.{time_to_string(duration[0])}.{duration[1]}"

        else:
            raise DatastarConfigError(
                f"Invalid duration configuration for on_interval: {duration}",
                context={
                    "duration_value": str(duration),
                    "duration_type": type(duration).__name__,
                },
                help_text="Duration must be a time value (int/float/str) or a tuple with time and 'leading' modifier.",
                example="""from stario.datastar import Datastar

ds = Datastar()

# Simple duration (time only):
ds.on_interval("action()", duration=1000)      # 1000ms (1 second)
ds.on_interval("action()", duration="5s")      # 5 seconds
ds.on_interval("action()", duration=2.5)       # 2.5 seconds

# With leading modifier (fires immediately, then at intervals):
ds.on_interval("action()", duration=(1000, "leading"))""",
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
        # Filter
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> dict[str, str]:
        """
        Execute an expression when signals are patched from the backend.

        Triggers when server-sent events (SSE) update signals. Can be filtered
        to only react to specific signal changes.

        Reference: https://data-star.dev/reference/attributes#data-on-signal-patch

        Args:
            expression: Datastar expression to execute
            delay: Delay before executing expression
            debounce: Debounce configuration
            throttle: Throttle configuration
            include: Only trigger for signals matching this pattern
            exclude: Don't trigger for signals matching this pattern

        Returns:
            Dictionary with data-on-signal-patch attribute(s)

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.on_signal_patch("console.log('updated')")
            {'data-on-signal-patch': "console.log('updated')"}
            >>> attrs.on_signal_patch("$log()", include="user.*", debounce=0.5)
            {'data-on-signal-patch__debounce.500ms': '$log()', 'data-on-signal-patch-filter': '{"include":"user.*"}'}
        """
        modifiers: list[str] = []
        append = modifiers.append

        if delay is not None:
            append("delay." + time_to_string(delay))

        if debounce is not None:
            append(debounce_to_string(debounce))

        if throttle is not None:
            append(throttle_to_string(throttle))

        if len(modifiers) > 0:
            key = "data-on-signal-patch__" + "__".join(modifiers)
        else:
            key = "data-on-signal-patch"

        if include is not None or exclude is not None:
            filter_dict = {}
            if include is not None:
                filter_dict["include"] = include
            if exclude is not None:
                filter_dict["exclude"] = exclude

            return {
                key: expression,
                "data-on-signal-patch-filter": quick_js_dump(filter_dict),
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
        """
        Execute an expression in response to a DOM event.

        Attaches an event listener to the element and executes the expression
        when the event fires. Supports all standard DOM events and custom events.

        Reference: https://data-star.dev/reference/attributes#data-on

        Args:
            event: Event name (click, input, submit, etc.) or custom event name
            expression: Datastar expression to execute
            once: Fire only once, then remove the listener
            passive: Mark listener as passive (improves scroll performance)
            capture: Use capture phase instead of bubble phase
            delay: Delay before executing expression
            debounce: Debounce configuration
            throttle: Throttle configuration
            viewtransition: Use view transitions for updates
            window: Attach listener to window instead of element
            outside: Trigger when event occurs outside the element
            prevent: Call preventDefault() on the event
            stop: Call stopPropagation() on the event

        Returns:
            Dictionary with data-on-{event} attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.on("click", "$count++")
            {'data-on:click': '$count++'}
            >>> attrs.on("submit", "$save()", prevent=True)
            {'data-on:submit__prevent': '$save()'}
            >>> attrs.on("input", "$search()", debounce=0.3)
            {'data-on:input__debounce.300ms': '$search()'}
            >>> attrs.on("click", "$close()", outside=True)
            {'data-on:click__outside': '$close()'}
            >>> attrs.on("scroll", "$track()", window=True, throttle=0.1)
            {'data-on:scroll__window__throttle.100ms': '$track()'}
        """
        modifiers = []
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
        if from_case != "kebab":  # Default
            append("case." + from_case)

        if len(modifiers) > 0:
            mods = "__".join(modifiers)
            return {f"data-on:{kebab_event}__{mods}": expression}

        return {f"data-on:{kebab_event}": expression}

    def preserve_attr(self, attrs: str | list[str]) -> dict[str, str]:
        """
        Preserve client-side attribute values during DOM morphing.

        Prevents specified attributes from being overwritten when elements
        are morphed/updated from backend responses. Useful for maintaining
        client-side state like focus or scroll position.

        Reference: https://data-star.dev/reference/attributes#data-preserve-attr

        Args:
            attrs: Attribute name(s) to preserve

        Returns:
            Dictionary with data-preserve-attr attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.preserve_attr("value")
            {'data-preserve-attr': 'value'}
            >>> attrs.preserve_attr(["value", "checked", "disabled"])
            {'data-preserve-attr': 'value checked disabled'}
        """
        value = attrs if isinstance(attrs, str) else " ".join(attrs)
        return {"data-preserve-attr": value}

    def ref(self, signal_name: str) -> dict[str, str]:
        """
        Create a signal that references the DOM element.

        Stores a reference to the element in a signal, allowing access to
        the DOM node from Datastar expressions and effects.

        Reference: https://data-star.dev/reference/attributes#data-ref

        Args:
            signal_name: Name of the signal to store the element reference

        Returns:
            Dictionary with data-ref attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.ref("inputEl")
            {'data-ref': 'inputEl'}
            >>> attrs.ref("modalElement")
            {'data-ref': 'modalElement'}
        """
        return {"data-ref": signal_name}

    def show(self, expression: str) -> dict[str, str]:
        """
        Show or hide an element based on a boolean expression.

        When the expression evaluates to true, the element is visible;
        when false, it's hidden (display: none). Reactively updates when
        signals change.

        Reference: https://data-star.dev/reference/attributes#data-show

        Args:
            expression: Boolean Datastar expression

        Returns:
            Dictionary with data-show attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.show("$isVisible")
            {'data-show': '$isVisible'}
            >>> attrs.show("$count > 0")
            {'data-show': '$count > 0'}
        """
        return {"data-show": expression}

    def signals(
        self,
        signals_dict: dict[str, SignalValue],
        *,
        ifmissing: bool = False,
    ) -> dict[str, str]:
        """
        Initialize signals with values.

        Creates or updates signals with specified initial values. Signals
        are reactive data stores that trigger updates when changed.

        Reference: https://data-star.dev/reference/attributes#data-signals

        Args:
            signals_dict: Dictionary mapping signal names to initial values
            ifmissing: If True, only set values for signals that don't exist

        Returns:
            Dictionary with data-signals or data-signals__ifmissing attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.signals({"count": "0", "name": "John"})
            {'data-signals': '{"count":"0","name":"John"}'}
            >>> attrs.signals({"fallback": "default"}, ifmissing=True)
            {'data-signals__ifmissing': '{"fallback":"default"}'}
        """
        if ifmissing:
            return {
                "data-signals__ifmissing": json.dumps(
                    signals_dict, separators=(",", ":")
                )
            }
        return {"data-signals": json.dumps(signals_dict, separators=(",", ":"))}

    def style(self, style_dict: dict[str, str]) -> dict[str, str]:
        """
        Set CSS styles based on expressions.

        Each key-value pair represents a CSS property and an expression for
        its value. Styles update reactively when signals change.

        Reference: https://data-star.dev/reference/attributes#data-style

        Args:
            style_dict: Dictionary mapping CSS properties to expressions

        Returns:
            Dictionary with data-style attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.style({"color": "$themeColor", "opacity": "$alpha"})
            {'data-style': '{"color":"$themeColor","opacity":"$alpha"}'}
        """
        return {"data-style": quick_js_dump(style_dict)}

    def text(self, expression: str) -> dict[str, str]:
        """
        Set the text content of an element from an expression.

        Updates the element's text content reactively when the expression
        changes. Automatically escapes HTML to prevent XSS.

        Reference: https://data-star.dev/reference/attributes#data-text

        Args:
            expression: Datastar expression evaluating to a string

        Returns:
            Dictionary with data-text attribute

        Examples:
            >>> attrs = DatastarAttributes()
            >>> attrs.text("$message")
            {'data-text': '$message'}
            >>> attrs.text("$firstName + ' ' + $lastName")
            {'data-text': "$firstName + ' ' + $lastName"}
        """
        return {"data-text": expression}


class DatastarActions:
    """
    Generator for Datastar actions.

    This class provides methods to generate Datastar actions. Some actions
    (HTTP methods) require the Stario application for URL resolution.

    Reference: https://data-star.dev/reference/actions

    Attributes:
        app: The Stario application instance for URL resolution

    Examples:
        >>> from stario.application import Stario
        >>> from stario.datastar import DatastarActions
        >>> app = Stario()
        >>> actions = DatastarActions(app)
        >>> actions.peek("$count")
        '@peek($count)'
        >>> actions.get("/api/users")
        "@get('/api/users')"
    """

    def __init__(self, app: Stario) -> None:
        """
        Initialize the DatastarActions generator.

        Args:
            app: Stario application instance for endpoint name -> URL resolution
        """
        self.app = app

    def peek(self, callable_expr: str) -> str:
        """
        Access signals without subscribing to their changes.

        Reads signal values without triggering re-evaluation when those signals
        change. Useful for one-time reads or breaking circular dependencies.

        Reference: https://data-star.dev/reference/actions#peek

        Args:
            callable_expr: Expression to evaluate without creating subscriptions

        Returns:
            Datastar @peek() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.peek("$count")
            '@peek($count)'
            >>> actions.peek("$user.name")
            '@peek($user.name)'
        """
        return f"@peek({callable_expr})"

    def set_all(
        self,
        value: str,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> str:
        """
        Set the value of multiple signals at once.

        Updates all matching signals (or all signals if no filter specified)
        to the same value. Useful for bulk resets or initializations.

        Reference: https://data-star.dev/reference/actions#setall

        Args:
            value: Expression for the new value
            include: Regex pattern or collection to match signal names to include
            exclude: Regex pattern or collection to match signal names to exclude

        Returns:
            Datastar @setAll() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.set_all("0")
            '@setAll(0)'
            >>> actions.set_all("false", include="is.*")
            '@setAll(false, {"include":"is.*"})'
            >>> actions.set_all("''", exclude=["id", "token"])
            '@setAll(\\'\\', {"exclude":"id|token"})'
        """
        if include is not None or exclude is not None:
            filter_dict = {}
            if include is not None:
                filter_dict["include"] = parse_filter_value(include)
            if exclude is not None:
                filter_dict["exclude"] = parse_filter_value(exclude)
            filter_json = quick_js_dump(filter_dict)

            return f"@setAll({value}, {filter_json})"
        return f"@setAll({value})"

    def toggle_all(
        self, include: FilterValue | None = None, exclude: FilterValue | None = None
    ) -> str:
        """
        Toggle the boolean value of multiple signals.

        Flips boolean values of all matching signals (or all signals if no
        filter specified). True becomes false, false becomes true.

        Reference: https://data-star.dev/reference/actions#toggleall

        Args:
            include: Regex pattern or collection to match signal names to include
            exclude: Regex pattern or collection to match signal names to exclude

        Returns:
            Datastar @toggleAll() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.toggle_all()
            '@toggleAll()'
            >>> actions.toggle_all(include="is.*")
            '@toggleAll({"include":"is.*"})'
            >>> actions.toggle_all(exclude=["isAdmin", "isRoot"])
            '@toggleAll({"exclude":"isAdmin|isRoot"})'
        """
        if include is not None or exclude is not None:
            filter_dict = {}
            if include is not None:
                filter_dict["include"] = parse_filter_value(include)
            if exclude is not None:
                filter_dict["exclude"] = parse_filter_value(exclude)
            filter_json = quick_js_dump(filter_dict)

            return f"@toggleAll({filter_json})"
        return "@toggleAll()"

    def _http_action(
        self,
        method: str,
        uri: str,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """
        Internal: Generate HTTP action strings for fetch requests.

        Base method used by get(), post(), put(), patch(), and delete() to
        generate properly formatted Datastar HTTP action strings with options.

        Args:
            method: HTTP method (get, post, put, patch, delete)
            uri: Endpoint URL or name (resolved via app.url_path_for if no leading /)
            content_type: Request content type (json or form)
            include: Signal filter for inclusion in request
            exclude: Signal filter for exclusion from request
            selector: CSS selector for target elements to update
            headers: Additional HTTP headers
            open_when_hidden: Open SSE connection even when page is hidden
            retry_interval_ms: Initial retry interval in milliseconds
            retry_scaler: Multiplier for exponential backoff
            retry_max_wait_ms: Maximum retry interval
            retry_max_count: Maximum number of retries
            request_cancellation: Request cancellation strategy

        Returns:
            Formatted Datastar HTTP action string
        """
        options = []

        # Only add content_type if it's not the default
        if content_type != "json":
            options.append(f"contentType: '{content_type}'")

        # Handle include/exclude filters
        if include is not None or exclude is not None:
            filter_dict = {}
            if include is not None:
                filter_dict["include"] = parse_filter_value(include)
            if exclude is not None:
                filter_dict["exclude"] = parse_filter_value(exclude)

            filter_json = quick_js_dump(filter_dict)
            options.append(f"filterSignals: {filter_json}")

        if selector is not None:
            options.append(f"selector: '{selector}'")

        if headers is not None:
            headers_json = quick_js_dump(headers)
            options.append(f"headers: {headers_json}")

        if open_when_hidden:
            options.append("openWhenHidden: true")

        # Only add retry options if they're not the defaults
        if retry_interval_ms != 1_000:
            options.append(f"retryInterval: {retry_interval_ms}")

        if retry_scaler != 2.0:
            options.append(f"retryScaler: {retry_scaler}")

        if retry_max_wait_ms != 30_000:
            options.append(f"retryMaxWaitMs: {retry_max_wait_ms}")

        if retry_max_count != 10:
            options.append(f"retryMaxCount: {retry_max_count}")

        if request_cancellation != "auto":
            options.append(f"requestCancellation: '{request_cancellation}'")

        if uri[0] != "/":
            # without leading slash, we assume this is the name of the endpoint
            uri = self.app.url_path_for(uri)

        if options:
            options_str = "{" + ", ".join(options) + "}"
            return f"@{method}('{uri}', {options_str})"
        else:
            return f"@{method}('{uri}')"

    def get(
        self,
        uri: str,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """
        Send a GET request to fetch data from the backend.

        Initiates a GET request and processes the SSE response stream to
        update signals and morph DOM elements.

        Reference: https://data-star.dev/reference/actions#get

        Args:
            uri: Endpoint URL or route name
            content_type: Request content type
            include: Include only matching signals in request
            exclude: Exclude matching signals from request
            selector: CSS selector for elements to update
            headers: Additional HTTP headers
            open_when_hidden: Keep connection open when page is hidden
            retry_interval_ms: Initial retry delay
            retry_scaler: Exponential backoff multiplier
            retry_max_wait_ms: Maximum retry delay
            retry_max_count: Maximum retry attempts
            request_cancellation: Cancellation strategy

        Returns:
            Datastar @get() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.get("/api/users")
            "@get('/api/users')"
            >>> actions.get("/api/data", include="user.*")
            '@get(\\'/api/data\\', {filterSignals: {"include":"user.*"}})'
        """
        return self._http_action(
            "get",
            uri,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def post(
        self,
        uri: str,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """
        Send a POST request with signal data to the backend.

        Initiates a POST request with signals as payload, processes SSE response
        stream to update signals and morph DOM elements.

        Reference: https://data-star.dev/reference/actions#post

        Args:
            uri: Endpoint URL or route name
            content_type: Request content type ('json' or 'form')
            include: Include only matching signals in request
            exclude: Exclude matching signals from request
            selector: CSS selector for elements to update
            headers: Additional HTTP headers
            open_when_hidden: Keep connection open when page is hidden
            retry_interval_ms: Initial retry delay
            retry_scaler: Exponential backoff multiplier
            retry_max_wait_ms: Maximum retry delay
            retry_max_count: Maximum retry attempts
            request_cancellation: Cancellation strategy

        Returns:
            Datastar @post() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.post("/api/users")
            "@post('/api/users')"
            >>> actions.post("/api/form", content_type="form")
            "@post('/api/form', {contentType: 'form'})"
        """
        return self._http_action(
            "post",
            uri,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def put(
        self,
        uri: str,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """
        Send a PUT request to update resources on the backend.

        Reference: https://data-star.dev/reference/actions#put

        Args:
            uri: Endpoint URL or route name
            (See post() for full parameter documentation)

        Returns:
            Datastar @put() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.put("/api/users/123")
            "@put('/api/users/123')"
        """
        return self._http_action(
            "put",
            uri,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def patch(
        self,
        uri: str,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """
        Send a PATCH request to partially update resources on the backend.

        Reference: https://data-star.dev/reference/actions#patch

        Args:
            uri: Endpoint URL or route name
            (See post() for full parameter documentation)

        Returns:
            Datastar @patch() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.patch("/api/users/123", include=["name", "email"])
            '@patch(\\'/api/users/123\\', {filterSignals: {"include":"name|email"}})'
        """
        return self._http_action(
            "patch",
            uri,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def delete(
        self,
        uri: str,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """
        Send a DELETE request to remove resources on the backend.

        Reference: https://data-star.dev/reference/actions#delete

        Args:
            uri: Endpoint URL or route name
            (See post() for full parameter documentation)

        Returns:
            Datastar @delete() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.delete("/api/users/123")
            "@delete('/api/users/123')"
        """
        return self._http_action(
            "delete",
            uri,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    # Pro Actions (require Datastar Pro)
    def clipboard(self, text: str, is_base64: bool = False) -> str:
        """
        Copy text to the system clipboard.

        Requires Datastar Pro. Copies text to the clipboard using the Clipboard API.

        Reference: https://data-star.dev/reference/actions#clipboard

        Args:
            text: Text to copy to clipboard
            is_base64: If True, text is base64 encoded

        Returns:
            Datastar @clipboard() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.clipboard("Hello World")
            "@clipboard('Hello World')"
            >>> actions.clipboard("SGVsbG8=", is_base64=True)
            "@clipboard('SGVsbG8=', true)"
        """
        if is_base64:
            return f"@clipboard('{text}', true)"
        return f"@clipboard('{text}')"

    def fit(
        self,
        v: str,
        old_min: float,
        old_max: float,
        new_min: float,
        new_max: float,
        should_clamp: bool = False,
        should_round: bool = False,
    ) -> str:
        """
        Linearly interpolate a value from one range to another.

        Requires Datastar Pro. Maps a value from one numeric range to another,
        optionally clamping and rounding the result.

        Reference: https://data-star.dev/reference/actions#fit

        Args:
            v: Value expression to interpolate
            old_min: Minimum of original range
            old_max: Maximum of original range
            new_min: Minimum of target range
            new_max: Maximum of target range
            should_clamp: Clamp result to target range
            should_round: Round result to nearest integer

        Returns:
            Datastar @fit() action string

        Examples:
            >>> actions = DatastarActions(Stario())
            >>> actions.fit("$value", 0, 100, 0, 1)
            '@fit($value, 0, 100, 0, 1, false, false)'
            >>> actions.fit("$percent", 0, 100, 0, 255, should_clamp=True, should_round=True)
            '@fit($percent, 0, 100, 0, 255, true, true)'
        """
        return f"@fit({v}, {old_min}, {old_max}, {new_min}, {new_max}, {str(should_clamp).lower()}, {str(should_round).lower()})"


type Attributes = Annotated[DatastarAttributes, DatastarAttributes, "singleton"]
"""
Type alias for the DatastarAttributes singleton dependency.

Use this type for dependency injection or type hints when you want to access
the DatastarAttributes generator, which provides methods to build Datastar HTML attributes
for your HTML elements.

Example (building an HTML element with Datastar attributes):

    from stario.html import input
    def my_input(attrs: Attributes):
        return input(attrs.bind("username"))

This type alias ensures consistent usage and discoverability throughout your
application and extensions.
"""


type Actions = Annotated[DatastarActions, DatastarActions, "singleton"]
"""
Type alias for the DatastarActions singleton dependency.

Use this type for dependency injection or type hints when you want to access
the DatastarActions generator, which provides methods to build Datastar actions
for your HTML elements.

Example (building an HTML element with Datastar actions):

    from stario.html import button
    def my_button(attr: Attributes, act: Actions):
        return button(
            attr.on("click", act.get("some_route")),
            "Click me",
        )

This type alias ensures consistent usage and discoverability throughout your
application and extensions.
"""
