"""
Rich Tracer - Beautiful console output for development.

Uses a background thread to batch updates every 0.1s.
Open spans render live, closed spans print and get cleaned up.
"""

import threading
import time
from datetime import datetime
from typing import Any
from uuid import UUID

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.traceback import Traceback

from .core import Event, Span


def _fmt_duration(ns: int) -> str:
    """Format nanoseconds as duration with unit."""
    ms = ns / 1e6
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, seconds = divmod(int(ms / 1000), 60)
    if minutes < 60:
        return f"{minutes}:{seconds:02d} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _get_status_code(span: Span) -> int | None:
    """Extract HTTP status code from span attributes."""
    for key in (
        "response.status_code",
        "status_code",
    ):
        if key in span.attributes:
            try:
                return int(span.attributes[key])
            except (ValueError, TypeError):
                pass
    return None


def _border_color(span: Span) -> str:
    """Determine border color based on span state."""
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


def _span_border_color(span: Span) -> str:
    """Get border color for nested span based on status."""
    if span.in_progress:
        return "grey50"
    if span.failed:
        return "red"
    return "green"


def _build_indent(indent_parts: list[tuple[str, str]]) -> Text:
    """Build styled Text from indent parts (text, style) tuples."""
    txt = Text()
    for text, style in indent_parts:
        txt.append(text, style=style)
    return txt


def _group_attributes(attrs: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """
    Group and format attributes by common prefix for compact tree display.

    All dotted keys are grouped by their prefix (all-but-last segment).
    Keys without dots are shown as flat entries.

    Returns list of (display_key, value, is_header) tuples:
    - Headers: (prefix, "", True) - group header with no value
    - Values: (".suffix", value, False) - indented suffix with value
    - Flat: (full_key, value, False) - standalone key (no dot prefix)

    Example:
        server.graceful_timeout: 5.0         →  server                (header)
        server.workers: 1                          .graceful_timeout  5.0
        server.worker.0.connections: 1             .workers           1
        response.status_code: 200                server.worker.0      (header)
        hello: world                               .connections       1
                                                 response             (header)
                                                   .status_code       200
                                                 hello                world (flat)
    """
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
    """Pretty console output with batched rendering every 0.1s."""

    __slots__ = (
        "console",
        "_roots",
        "_children",
        "_dirty",
        "_lock",
        "_thread",
        "_running",
        "_live",
        "_printed",
    )

    def __init__(self) -> None:
        self.console = Console()
        self._roots: dict[UUID, Span] = {}  # Root spans by ID
        self._children: dict[UUID, list[Span]] = {}  # Children by parent_id
        self._dirty: set[Span] = set()  # Spans needing update (double-buffer)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._live: Live | None = None
        self._printed: set[UUID] = set()  # Track printed spans to avoid duplicates

    def __enter__(self) -> "RichTracer":
        self._start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop()

    def __call__(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        """Create a new root span."""
        if not self._running:
            self._start()
        return Span(self, name, attributes)

    def notify(self, span: Span) -> None:
        """Mark span as needing update (lock-free)."""
        self._dirty.add(span)

    def _start(self) -> None:
        """Start background render thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _stop(self) -> None:
        """Stop and render any remaining spans."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._flush_remaining()

    def flush(self) -> None:
        """Force render of all pending spans. Call before program exit."""
        # Give background thread time to pick up recent spans
        time.sleep(0.15)
        # Then flush anything remaining
        self._flush_remaining()

    def _flush_remaining(self) -> None:
        """Process and print any remaining dirty spans."""
        if self._live:
            self._live.stop()
            self._live = None
        # Process any remaining dirty spans
        for span in self._dirty:
            if span.parent_id is None:
                self._roots[span.id] = span
        # Print remaining roots (skip already printed)
        for span in self._roots.values():
            if span.id not in self._printed:
                self.console.print(self._panel(span))
        self._roots.clear()
        self._children.clear()
        self._dirty.clear()
        self._printed.clear()

    def _loop(self) -> None:
        """Background thread: render every 0.1s."""
        while self._running:
            start = time.perf_counter()

            # Swap dirty set (double-buffer, only lock during swap)
            with self._lock:
                dirty = self._dirty
                self._dirty = set()

            if dirty:
                # Update tracking from dirty spans (now single-threaded)
                for span in dirty:
                    if span.parent_id is None:
                        self._roots[span.id] = span
                    else:
                        self._children.setdefault(span.parent_id, [])
                        if span not in self._children[span.parent_id]:
                            self._children[span.parent_id].append(span)
                self._render()

            # Sleep remaining time to maintain 0.1s interval
            elapsed = time.perf_counter() - start
            if (remaining := 0.1 - elapsed) > 0:
                time.sleep(remaining)

    def _render(self) -> None:
        """Render: print closed roots, live-update open ones."""
        # Stop live before printing (avoids duplicate output)
        if self._live:
            self._live.stop()
            self._live = None

        # Print and cleanup closed roots
        for span_id in list(self._roots.keys()):
            span = self._roots[span_id]
            if span.finished and span_id not in self._printed:
                self.console.print(self._panel(span))
                self._printed.add(span_id)
                self._cleanup(span_id)

        # Live display for open roots
        open_roots = [s for s in self._roots.values() if s.in_progress]
        if open_roots:
            self._live = Live(
                Group(*[self._panel(s) for s in open_roots]),
                console=self.console,
                transient=True,
            )
            self._live.start()

    def _cleanup(self, span_id: UUID) -> None:
        """Remove span and its children from tracking."""
        self._roots.pop(span_id, None)
        for child in self._children.pop(span_id, []):
            self._cleanup(child.id)

    def _panel(self, span: Span) -> Panel:
        """Build panel for a root span."""
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

    def _root_content(self, span: Span) -> RenderableType:
        """Build content for root span panel."""
        parts: list[RenderableType] = []

        # Grouped attributes in 2-column layout
        if span.attributes:
            parts.append(self._attributes_table(span.attributes))

        # Events and child spans sorted by time
        nested = self._nested_items(span)
        if nested:
            parts.append(nested)

        return Group(*parts) if len(parts) > 1 else (parts[0] if parts else Text(""))

    def _attributes_table(self, attrs: dict[str, Any]) -> Table:
        """Build 2-column table with grouped attributes in compact tree format."""
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

    def _nested_items(self, span: Span) -> RenderableType | None:
        """Build nested events and child spans sorted by timestamp."""
        items: list[tuple[int, Event | Span]] = []
        items.extend((e.time_ns, e) for e in span.events)
        items.extend((c.start_ns, c) for c in self._children.get(span.id, []))

        if not items:
            return None

        items.sort(key=lambda x: x[0])
        parts: list[RenderableType] = []

        for _, item in items:
            if isinstance(item, Event):
                # Root level events - no indent
                parts.append(self._event_block(item, span.start_ns, indent_parts=[]))
            else:
                parts.append(
                    self._nested_span_block(item, span.start_ns, indent_parts=[])
                )

        return Group(*parts)

    def _event_block(
        self,
        event: Event,
        parent_start: int,
        indent_parts: list[tuple[str, str]] | None = None,
    ) -> RenderableType:
        """
        Render an event: ◆ +time name  key: value key: value
        Or for exceptions: ✗ +time exception  type: X  message: Y + traceback

        Body content (tracebacks, text output) is rendered below the header
        with a continuation border.

        Args:
            indent_parts: List of (text, style) tuples for colored left border
        """
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
        span: Span,
        parent_start: int,
        indent_parts: list[tuple[str, str]] | None = None,
    ) -> RenderableType:
        """
        Render a nested span: ● +time name (duration)
        With colored left border for nested content (attributes, events, sub-spans).

        Args:
            indent_parts: List of (text, style) tuples for colored left border

        Structure:
        ● +0 ms db.connect (55 ms)
        │ db.host: localhost          (│ colored by span status)
        │ db.type: postgres
        │ ◆ +10 ms query.executed  rows: 1
        │ ● +20 ms sub.span (30 ms)
        │ │ ...nested content...      (inner │ colored by sub.span status)
        """
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
        if span.attributes:
            for display_key, value, is_header in _group_attributes(span.attributes):
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
        children = self._children.get(span.id, [])
        items: list[tuple[int, Event | Span]] = []
        items.extend((e.time_ns, e) for e in span.events)
        items.extend((c.start_ns, c) for c in children)

        if items:
            items.sort(key=lambda x: x[0])
            for _, item in items:
                if isinstance(item, Event):
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
