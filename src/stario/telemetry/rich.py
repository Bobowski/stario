"""Rich console tracer with live span rendering."""

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid7

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.traceback import Traceback

from .core import Span

_REFRESH_INTERVAL_S = 0.125


@dataclass(slots=True)
class _RichState:
    id: UUID
    name: str
    parent_id: UUID | None
    start_ns: int
    end_ns: int | None = None
    error: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    events: list["_RichEvent"] = field(default_factory=list)
    links: list["_RichLink"] = field(default_factory=list)

    @property
    def started(self) -> bool:
        return self.start_ns != 0

    @property
    def attributes_for_tracer(self) -> Mapping[str, Any]:
        return MappingProxyType(self.attrs)

    @property
    def events_for_tracer(self) -> tuple["_RichEvent", ...]:
        return tuple(self.events)

    @property
    def duration_ns(self) -> int | None:
        if self.end_ns is None:
            return None
        return self.end_ns - self.start_ns

    @property
    def in_progress(self) -> bool:
        return self.end_ns is None

    @property
    def finished(self) -> bool:
        return self.end_ns is not None

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class _RichEvent:
    time_ns: int
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    body: Any | None = None


@dataclass(slots=True, frozen=True)
class _RichLink:
    span_id: UUID
    attributes: dict[str, Any] = field(default_factory=dict)


def _fmt_duration(ns: int) -> str:
    """Format nanoseconds for display."""
    if ns < 1_000_000:
        us = ns / 1e3
        if us < 10:
            return f"{us:.1f} us"
        return f"{us:.0f} us"

    ms = ns / 1e6
    if ms < 10:
        return f"{ms:.1f} ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, seconds = divmod(int(ms / 1000), 60)
    if minutes < 60:
        return f"{minutes}:{seconds:02d} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _get_status_code(span: _RichState) -> int | None:
    """Read status code from known tag keys."""
    attrs = span.attributes_for_tracer
    for key in (
        "response.status_code",
        "status_code",
    ):
        if key in attrs:
            try:
                return int(attrs[key])
            except (ValueError, TypeError):
                pass
    return None


def _border_color(span: _RichState) -> str:
    """Choose color for root span."""
    if span.in_progress:
        return "grey50"

    if span.failed:
        return "red"

    # Check status code for root spans
    status = _get_status_code(span)
    if status is not None:
        if 200 <= status < 300:
            return "green"
        if 300 <= status < 500:
            return "yellow"
        if status >= 500:
            return "red"

    # Default: green for ok, red for error
    return "green" if span.ok else "red"


def _span_border_color(span: _RichState) -> str:
    """Choose color for nested span."""
    if span.in_progress:
        return "grey50"
    if span.failed:
        return "red"
    return "green"


def _build_indent(indent_parts: list[tuple[str, str]]) -> Text:
    """Build styled indent text."""
    txt = Text()
    for text, style in indent_parts:
        txt.append(text, style=style)
    return txt


def _group_attributes(attrs: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    """Group dotted keys by prefix for compact output."""
    if not attrs:
        return []

    # Group by prefix (all but last dot segment)
    grouped: dict[str, list[tuple[str, str]]] = {}  # prefix -> [(suffix, value), ...]
    flat: list[tuple[str, str]] = []  # [(key, value), ...] for keys without dots

    for key, value in attrs.items():
        parts = key.rsplit(".", 1)
        if len(parts) == 2:
            prefix, suffix = parts
            grouped.setdefault(prefix, []).append((suffix, str(value)))
        else:
            flat.append((key, str(value)))

    # Build result: prefixes sorted, then flat keys sorted
    result: list[tuple[str, str, bool]] = []

    for prefix in sorted(grouped.keys()):
        # Header for the prefix
        result.append((prefix, "", True))
        # Values sorted by suffix
        for suffix, value in sorted(grouped[prefix]):
            result.append((f".{suffix}", value, False))

    # Flat keys (no dots) - sorted, not indented
    for key, value in sorted(flat):
        result.append((key, value, False))

    return result


class RichTracer:
    """Render spans live in terminal panels."""

    __slots__ = (
        "console",
        "_spans",
        "_roots",
        "_children",
        "_lock",
        "_thread",
        "_running",
        "_dirty",
        "_live",
    )

    def __init__(self) -> None:
        self.console = Console()
        self._spans: dict[UUID, _RichState] = {}
        self._roots: dict[UUID, _RichState] = {}  # Root spans by ID
        self._children: dict[UUID, list[_RichState]] = {}  # Children by parent_id
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._dirty = False
        self._live: Live | None = None

    def __enter__(self) -> "RichTracer":
        self._start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop()

    def create(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        parent_id: UUID | None = None,
    ) -> Span:
        """Create a stopped span handle."""
        if not self._running:
            self._start()
        span = Span(id=uuid7(), tracer=self)
        state = _RichState(
            id=span.id,
            name=name,
            parent_id=parent_id,
            start_ns=0,
            attrs=attributes.copy() if attributes else {},
        )
        with self._lock:
            self._spans[span.id] = state
            if parent_id is None:
                self._roots[span.id] = state
            else:
                self._children.setdefault(parent_id, [])
                if state not in self._children[parent_id]:
                    self._children[parent_id].append(state)
        return span

    def start(self, span_id: UUID) -> None:
        """Mark span as started if this is first start call."""
        with self._lock:
            if state := self._spans.get(span_id):
                if state.start_ns == 0:
                    state.start_ns = time.time_ns()
                    self._dirty = True

    def set_attribute(
        self,
        span_id: UUID,
        name: str,
        value: Any,
    ) -> None:
        with self._lock:
            if state := self._spans.get(span_id):
                state.attrs[name] = value
                self._dirty = True

    def set_attributes(
        self,
        span_id: UUID,
        attributes: dict[str, Any],
    ) -> None:
        if not attributes:
            return
        with self._lock:
            if state := self._spans.get(span_id):
                state.attrs.update(attributes)
                self._dirty = True

    def add_event(
        self,
        span_id: UUID,
        name: str,
        attributes: dict[str, Any] | None = None,
        *,
        body: Any | None = None,
    ) -> None:
        event = _RichEvent(
            time_ns=time.time_ns(),
            name=name,
            attributes=attributes.copy() if attributes else {},
            body=body,
        )
        with self._lock:
            if state := self._spans.get(span_id):
                state.events.append(event)
                self._dirty = True

    def add_link(
        self,
        span_id: UUID,
        target_span_id: UUID,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        link = _RichLink(
            span_id=target_span_id, attributes=attributes.copy() if attributes else {}
        )
        with self._lock:
            if state := self._spans.get(span_id):
                state.links.append(link)
                self._dirty = True

    def fail(self, span_id: UUID, message: str) -> None:
        with self._lock:
            if state := self._spans.get(span_id):
                state.error = message
                self._dirty = True

    def set_name(self, span_id: UUID, name: str) -> None:
        with self._lock:
            if state := self._spans.get(span_id):
                state.name = name
                self._dirty = True

    def end(self, span_id: UUID) -> None:
        with self._lock:
            if state := self._spans.get(span_id):
                if state.start_ns == 0:
                    raise RuntimeError(
                        "Cannot end a span that was never started. "
                        "Call span.start() or use the span as a context manager."
                    )
                if state.end_ns is None:
                    state.end_ns = time.time_ns()
                    self._dirty = True

    def _start(self) -> None:
        """Start the fixed-interval render watcher."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._dirty = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _stop(self) -> None:
        """Stop watcher and flush pending output."""
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.flush()

    def flush(self) -> None:
        """Flush pending output."""
        with self._lock:
            self._stop_live_locked()
            remaining = [span for span in self._roots.values() if span.started]
            self._print_roots_locked(remaining)
            self._roots.clear()
            self._children.clear()
            self._spans.clear()
            self._dirty = False

    def _loop(self) -> None:
        """Render dirty state on a fixed interval."""
        while True:
            started_at = time.perf_counter()
            with self._lock:
                if not self._running:
                    return
                if self._dirty:
                    self._render_locked()

            if (remaining := _REFRESH_INTERVAL_S - (time.perf_counter() - started_at)) > 0:
                time.sleep(remaining)

    def _render(self) -> None:
        """Render one watcher tick."""
        with self._lock:
            self._render_locked()

    def _render_locked(self) -> None:
        """Print finished roots and keep only in-progress roots live."""
        closed_roots = [
            span for span in self._roots.values() if span.started and span.finished
        ]
        open_roots = [
            span for span in self._roots.values() if span.started and span.in_progress
        ]

        if open_roots or closed_roots or self._live is not None:
            renderable = self._live_renderable(open_roots)
            if self._live is None:
                self._live = Live(
                    renderable,
                    console=self.console,
                    transient=True,
                    auto_refresh=False,
                )
                self._live.start()
            else:
                self._live.update(renderable, refresh=True)

        if closed_roots:
            closed_panels = [(span, self._panel(span)) for span in closed_roots]
            self._print_roots_locked(closed_roots, panels=closed_panels)

        self._dirty = False

    def _live_renderable(self, open_roots: list[_RichState]) -> RenderableType:
        """Build the live region, keeping a single blank line when idle."""
        if not open_roots:
            return Text(" ")
        return Group(*(self._panel(span) for span in open_roots))

    def _stop_live_locked(self) -> None:
        """Stop the active live region, if any."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _print_roots_locked(
        self,
        spans: list[_RichState],
        *,
        panels: list[tuple[_RichState, Panel]] | None = None,
    ) -> None:
        """Print root panels and forget their tracked subtrees."""
        panel_map = {span.id: panel for span, panel in panels or []}
        for span in spans:
            self.console.print(panel_map.get(span.id) or self._panel(span))
            self._cleanup(span.id)

    def _cleanup(self, span_id: UUID) -> None:
        """Drop a root subtree from tracking."""
        self._roots.pop(span_id, None)
        self._spans.pop(span_id, None)
        for child in self._children.pop(span_id, []):
            self._cleanup(child.id)

    def _panel(self, span: _RichState) -> Panel:
        """Build root panel."""
        color = _border_color(span)
        time_str = datetime.fromtimestamp(span.start_ns / 1e9).strftime("%H:%M:%S.%f")[
            :-3
        ]

        # Build title with optional duration and error
        title = Text()
        title.append(f"{time_str} ", style="dim")
        title.append(f"{str(span.id)[-8:]} ", style="dim")
        title.append(span.name, style="white")

        # Duration in header for finished spans
        if span.duration_ns:
            title.append(f" ({_fmt_duration(span.duration_ns)})", style="dim")

        # Error in header (same style as nested spans)
        if span.error:
            title.append("  error: ", style="dim")
            title.append(span.error, style="red")

        return Panel(
            self._root_content(span),
            title=title,
            title_align="left",
            border_style=color,
            padding=(0, 1),
        )

    def _root_content(self, span: _RichState) -> RenderableType:
        """Build root panel body."""
        parts: list[RenderableType] = []

        # Grouped attributes in 2-column layout
        attrs = span.attributes_for_tracer
        if attrs:
            parts.append(self._attributes_table(attrs))

        # Events and child spans sorted by time
        nested = self._nested_items(span)
        if nested:
            parts.append(nested)

        return Group(*parts) if len(parts) > 1 else (parts[0] if parts else Text(""))

    def _attributes_table(self, attrs: Mapping[str, Any]) -> Table:
        """Build two-column attributes table."""
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)  # Keys in dim
        table.add_column(style="white")  # Values in white

        for display_key, value, is_header in _group_attributes(attrs):
            if is_header:
                # Group header - just the prefix, no colon, no value
                table.add_row(display_key, "")
            elif display_key.startswith("."):
                # Value row under a header - indented with .suffix
                table.add_row(f"  {display_key}:", value)
            else:
                # Flat key - no dot prefix, no extra indent
                table.add_row(f"{display_key}:", value)

        return table

    def _nested_items(self, span: _RichState) -> RenderableType | None:
        """Build nested children and events sorted by time."""
        items: list[tuple[int, _RichEvent | _RichState]] = []
        items.extend((e.time_ns, e) for e in span.events_for_tracer)
        items.extend(
            (c.start_ns, c) for c in self._children.get(span.id, []) if c.started
        )

        if not items:
            return None

        items.sort(key=lambda x: x[0])
        parts: list[RenderableType] = []

        for _, item in items:
            if isinstance(item, _RichEvent):
                # Root level events - no indent
                parts.append(self._event_block(item, span.start_ns, indent_parts=[]))
            else:
                parts.append(
                    self._nested_span_block(item, span.start_ns, indent_parts=[])
                )

        return Group(*parts)

    def _event_block(
        self,
        event: _RichEvent,
        parent_start: int,
        indent_parts: list[tuple[str, str]] | None = None,
    ) -> RenderableType:
        """Render one event line (plus optional body)."""
        indent_parts = indent_parts or []
        body = event.body
        is_exception = isinstance(body, BaseException)

        # Build header line: symbol +time name  key: value key: value
        txt = Text()
        if indent_parts:
            txt.append_text(_build_indent(indent_parts))

        if is_exception:
            # Exception event - use ✗ symbol and red name
            txt.append("✗ ", style="red")
            txt.append(f"+{_fmt_duration(event.time_ns - parent_start)} ", style="dim")
            txt.append(event.name, style="red")
        else:
            # Regular event - use ◆ symbol and white name
            txt.append("◆ ", style="cyan")
            txt.append(f"+{_fmt_duration(event.time_ns - parent_start)} ", style="dim")
            txt.append(event.name, style="white")

        # Event attributes: key: value
        if event.attributes:
            txt.append("  ")
            for i, (k, v) in enumerate(event.attributes.items()):
                if i > 0:
                    txt.append(" ")
                txt.append(f"{k}: ", style="dim")
                txt.append(str(v), style="white")

        # If no body, return just the header line
        if body is None:
            return txt

        # Body content - rendered below header with continuation border
        parts: list[RenderableType] = [txt]
        tb_border_color = indent_parts[-1][1] if indent_parts else "dim"

        if is_exception:
            # Exception body - render as rich traceback
            tb = getattr(body, "__traceback__", None)
            if tb and isinstance(body, BaseException):
                traceback_obj = Traceback.from_exception(
                    type(body), body, tb, max_frames=4
                )
                indent_width = sum(len(text) for text, _ in indent_parts)
                tb_width = max(80, self.console.width - indent_width - 4)
                temp_console = Console(
                    force_terminal=True, no_color=False, width=tb_width
                )
                with temp_console.capture() as capture:
                    temp_console.print(traceback_obj, end="")
                for line in capture.get().splitlines():
                    body_line = Text()
                    if indent_parts:
                        body_line.append_text(_build_indent(indent_parts))
                    body_line.append("│ ", style=tb_border_color)
                    body_line.append_text(Text.from_ansi(line))
                    parts.append(body_line)
        else:
            # Text body - render each line with continuation border
            body_str = str(body) if not isinstance(body, str) else body
            for line in body_str.splitlines():
                body_line = Text()
                if indent_parts:
                    body_line.append_text(_build_indent(indent_parts))
                body_line.append("│ ", style=tb_border_color)
                body_line.append(line, style="dim")
                parts.append(body_line)

        return Group(*parts) if len(parts) > 1 else parts[0]

    def _nested_span_block(
        self,
        span: _RichState,
        parent_start: int,
        indent_parts: list[tuple[str, str]] | None = None,
    ) -> RenderableType:
        """Render a nested span block recursively."""
        indent_parts = indent_parts or []

        # Shape and color based on status
        if span.in_progress:
            symbol, color = "○", "grey50"  # Hollow circle - incomplete
        elif span.failed:
            symbol, color = "✗", "red"  # Cross - failed
        else:
            symbol, color = "●", "green"  # Filled circle - success

        # Border color for this span's children
        border_color = _span_border_color(span)

        parts: list[RenderableType] = []

        # Header: indent + symbol +time name (duration)
        header = Text()
        if indent_parts:
            header.append_text(_build_indent(indent_parts))
        header.append(f"{symbol} ", style=color)
        header.append(f"+{_fmt_duration(span.start_ns - parent_start)} ", style="dim")
        header.append(span.name, style="white")
        if span.duration_ns:
            header.append(f" ({_fmt_duration(span.duration_ns)})", style="dim")
        if span.error:
            header.append("  error: ", style="dim")
            header.append(span.error, style="red")
        parts.append(header)

        # Child indent = current indent + colored "│ "
        child_indent_parts = indent_parts + [("│ ", border_color)]

        # Attributes with colored left border in compact tree format
        attrs = span.attributes_for_tracer
        if attrs:
            for display_key, value, is_header in _group_attributes(attrs):
                attr_line = Text()
                attr_line.append_text(_build_indent(child_indent_parts))
                if is_header:
                    # Group header - just the prefix
                    attr_line.append(f" {display_key}", style="dim")
                elif display_key.startswith("."):
                    # Value row under a header - indented .suffix: value
                    attr_line.append(f"   {display_key}:", style="dim")
                    attr_line.append(f"  {value}", style="white")
                else:
                    # Flat key - no dot prefix, no extra indent
                    attr_line.append(f" {display_key}:", style="dim")
                    attr_line.append(f"  {value}", style="white")
                parts.append(attr_line)

        # Events and child spans sorted by time
        children = [child for child in self._children.get(span.id, []) if child.started]
        items: list[tuple[int, _RichEvent | _RichState]] = []
        items.extend((e.time_ns, e) for e in span.events_for_tracer)
        items.extend((c.start_ns, c) for c in children)

        if items:
            items.sort(key=lambda x: x[0])
            for _, item in items:
                if isinstance(item, _RichEvent):
                    # Events get the child indent
                    event_block = self._event_block(
                        item, span.start_ns, indent_parts=child_indent_parts
                    )
                    parts.append(event_block)
                else:
                    # Recursive nested span with child indent
                    nested_block = self._nested_span_block(
                        item, span.start_ns, indent_parts=child_indent_parts
                    )
                    parts.append(nested_block)

        return Group(*parts) if len(parts) > 1 else parts[0]
