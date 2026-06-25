"""TTY span tree for interactive development (stdlib + ANSI).

Not a production log format. Finished roots scroll into history; open roots
repaint in a live footer (`_LiveRegion` uses cursor-up + clear). Nothing else
should write to the same output stream while the tracer is active.

`TTYRenderer` is the pure span→string layer and the unit-test seam: no locks,
threads, or I/O.
"""

import shutil
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from itertools import chain
from types import TracebackType
from typing import Any, TextIO, cast
from unicodedata import combining, east_asian_width
from uuid import UUID, uuid7

from stario._terminal import RESET as _RESET
from stario._terminal import SGR, color_enabled, enable_vt_for_stream

from .core import Attributes, Span, TelemetryStats
from .spans import RecordedEvent, RecordedLink, RecordingSpan

_REFRESH_INTERVAL_S = 0.125
_TIME_COL = 14
# Single-cell live placeholder so `splitlines()` still counts as one row.
_IDLE_LIVE = " "
_ELLIPSIS = "…"


def _styled(text: str, style: str) -> str:
    if not color_enabled():
        return text
    prefix = SGR.get(style)
    if not prefix:
        return text
    return f"{prefix}{text}{_RESET}"


def _ansi_sequence_end(text: str, index: int) -> int | None:
    if text[index : index + 2] != "\033[":
        return None
    for end in range(index + 2, len(text)):
        if "@" <= text[end] <= "~":
            return end + 1
    return None


def _cell_width(ch: str) -> int:
    if combining(ch):
        return 0
    return 2 if east_asian_width(ch) in {"F", "W"} else 1


def _visible_width(text: str) -> int:
    width = 0
    i = 0
    while i < len(text):
        if (end := _ansi_sequence_end(text, i)) is not None:
            i = end
            continue
        width += _cell_width(text[i])
        i += 1
    return width


def _clip_visible(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if _visible_width(text) <= max_width:
        return text

    ellipsis_width = _cell_width(_ELLIPSIS)
    if max_width <= ellipsis_width:
        return _ELLIPSIS[:max_width]

    budget = max_width - ellipsis_width
    width = 0
    i = 0
    pieces: list[str] = []
    saw_ansi = False
    while i < len(text):
        if (end := _ansi_sequence_end(text, i)) is not None:
            pieces.append(text[i:end])
            saw_ansi = True
            i = end
            continue
        ch = text[i]
        ch_width = _cell_width(ch)
        if width + ch_width > budget:
            break
        pieces.append(ch)
        width += ch_width
        i += 1

    pieces.append(_ELLIPSIS)
    if saw_ansi:
        pieces.append(_RESET)
    return "".join(pieces)


def _fmt_duration(ns: int) -> str:
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


def _uuid_tail(uid: UUID) -> str:
    s = str(uid)
    return s[-8:] if len(s) >= 8 else s


def _span_status_style(span: RecordingSpan) -> str:
    if span.in_progress:
        return "cyan"
    if span.failed:
        return "red"
    for key in ("response.status_code", "status_code"):
        if not span.attributes or key not in span.attributes:
            continue
        try:
            code = int(span.attributes[key])
        except ValueError, TypeError:
            continue
        if 200 <= code < 300:
            return "green"
        if 300 <= code < 500:
            return "yellow"
        return "red"
    return "green"


def _build_status_trailer(span: RecordingSpan, max_len: int) -> str:
    if span.in_progress:
        dur = "…"
    elif span.duration_ns is not None:
        dur = _fmt_duration(span.duration_ns)
    else:
        dur = "—"
    id_s = _uuid_tail(span.id)
    rest = f"{dur} {id_s}"
    if max_len <= 0:
        return ""
    if not span.failed:
        return rest[:max_len] if len(rest) > max_len else rest

    err = span.error or "failed"
    full = f"[{err}] {dur} {id_s}"
    if len(full) <= max_len:
        return full
    tail = f" {dur} {id_s}"
    budget = max_len - len(tail) - 2
    if budget < 1:
        candidate = f"[…]{tail}"
        return candidate[:max_len] if len(candidate) > max_len else candidate
    short = err if len(err) <= budget else f"{err[: budget - 1]}…"
    packed = f"[{short}]{tail}"
    if len(packed) <= max_len:
        return packed
    return packed[:max_len]


class _LiveRegion:
    """Bottom-of-terminal live block: erase previous lines, then caller prints history, then write."""

    __slots__ = ("_lines", "_lock", "_out")

    def __init__(self, out: Any, lock: threading.Lock) -> None:
        self._out = out
        self._lock = lock
        self._lines = 0

    def _erase_unlocked(self) -> None:
        self._clear_written_lines()
        self._out.flush()
        self._lines = 0

    def erase(self) -> None:
        """Move cursor up and clear the last live block (must be directly above current position)."""
        with self._lock:
            self._erase_unlocked()

    def write(self, content: str) -> None:
        with self._lock:
            written = content if content.endswith("\n") else content + "\n"
            self._out.write(written)
            self._out.flush()
            # `" ".splitlines()` is [] — still one screen row for the idle placeholder.
            self._lines = max(1, len(written.splitlines())) if written else 1

    def _clear_written_lines(self) -> None:
        for _ in range(self._lines):
            self._out.write("\x1b[1A\x1b[2K\r")

    def stop(self) -> None:
        with self._lock:
            self._erase_unlocked()


class TTYRenderer:
    """Pure string rendering for span trees; no I/O, locks, or threads.

    Pass terminal width and a `parent_id → children` map so nested spans and
    events sort by time. Tests build `RecordingSpan` records and drive this
    class directly.
    """

    __slots__ = ("_children", "_width")

    def __init__(self, width: int, children: dict[UUID, list[RecordingSpan]]) -> None:
        self._width = max(1, width)
        self._children = children

    def _fit_line(self, line: str) -> str:
        return _clip_visible(line, self._width)

    def root_block(self, span: RecordingSpan) -> str:
        return (
            self.root_separator_line(span)
            + "\n"
            + self.span_tree(span, parent_start_ns=None, indent_level=0)
        )

    def live_text(self, open_roots: list[RecordingSpan]) -> str:
        if not open_roots:
            return _IDLE_LIVE
        parts: list[str] = []
        for span in open_roots:
            parts.append(self.root_block(span))
        return "\n".join(parts)

    def root_separator_line(self, span: RecordingSpan) -> str:
        style = _span_status_style(span)
        return _styled("─" * self._width, style)

    def span_header_line(
        self,
        span: RecordingSpan,
        *,
        parent_start_ns: int | None,
        indent_level: int,
    ) -> str:
        indent = "  " * indent_level
        tw = self._width
        if span.start_ns is None:
            raise RuntimeError("TTYRenderer can only render started spans")
        if parent_start_ns is None:
            time_s = (
                datetime.fromtimestamp(span.start_ns / 1e9)
                .strftime("%H:%M:%S.%f")[:-3]
                .ljust(_TIME_COL)[:_TIME_COL]
            )
        else:
            offset_ns = span.start_ns - parent_start_ns
            time_s = f"+{_fmt_duration(offset_ns)}"[:_TIME_COL].ljust(_TIME_COL)

        prefix_len = len(indent) + _TIME_COL + 1
        style = _span_status_style(span)
        available = tw - prefix_len

        trailer = ""
        if available >= 16:
            trailer_budget = max(8, available // 2)
            trailer = _build_status_trailer(span, trailer_budget)

        name_w = max(0, available - len(trailer) - (1 if trailer else 0))
        name = _clip_visible(span.name, name_w)
        gap = max(0, available - _visible_width(name) - len(trailer))

        return self._fit_line(
            _styled(indent + time_s + " ", "dim")
            + _styled(name, "white")
            + _styled(" " * gap, "dim")
            + _styled(trailer, style)
        )

    def attribute_lines(self, attrs: Mapping[str, Any], base_level: int) -> list[str]:
        if not attrs:
            return []
        keys = sorted(attrs.keys())
        max_before_value = max(len(k) + 1 for k in keys)
        base = "  " * base_level
        lines: list[str] = []
        for key in keys:
            pad = max_before_value - len(key) - 1
            pad_s = " " * pad if pad > 0 else ""
            line = (
                base
                + _styled(key, "dim")
                + _styled(":", "dim")
                + _styled(pad_s, "dim")
                + " "
                + _styled(str(attrs[key]), "white")
            )
            lines.append(self._fit_line(line))
        return lines

    def link_lines(self, links: list[RecordedLink], base_level: int) -> list[str]:
        base = "  " * base_level
        lines: list[str] = []
        for link in links:
            tail = _uuid_tail(link.span_id)
            parts = base + _styled("link ", "dim") + _styled(link.name, "white")
            parts += _styled(f" {tail}", "dim")
            if link.attributes:
                parts += "  "
                for i, (k, v) in enumerate(link.attributes.items()):
                    if i:
                        parts += " "
                    parts += _styled(f"{k}=", "dim") + _styled(str(v), "white")
            lines.append(self._fit_line(parts))
        return lines

    def span_tree(
        self,
        span: RecordingSpan,
        *,
        parent_start_ns: int | None,
        indent_level: int,
    ) -> str:
        parts: list[str] = [
            self.span_header_line(
                span, parent_start_ns=parent_start_ns, indent_level=indent_level
            )
        ]
        attrs = span.attributes
        if attrs:
            parts.extend(self.attribute_lines(attrs, indent_level + 1))
        if span.links:
            parts.extend(self.link_lines(span.links, indent_level + 1))
        nested = self.nested_items(span, indent_level)
        if nested:
            parts.append(nested)
        return "\n".join(parts)

    def nested_items(self, span: RecordingSpan, indent_level: int) -> str | None:
        items: list[tuple[int, RecordedEvent | RecordingSpan]] = sorted(
            chain(
                ((e.time_ns, e) for e in span.events or []),
                (
                    (c.start_ns, c)
                    for c in self._children.get(span.id, [])
                    if c.start_ns is not None
                ),
            ),
            key=lambda x: x[0],
        )
        if not items:
            return None
        parent_start = span.start_ns
        if parent_start is None:
            return None
        child_level = indent_level + 1
        blocks: list[str] = []
        for _, item in items:
            if isinstance(item, RecordedEvent):
                blocks.append(
                    self.event_block(item, parent_start, indent_level=child_level)
                )
            else:
                blocks.append(
                    self.span_tree(
                        item, parent_start_ns=parent_start, indent_level=child_level
                    )
                )
        return "\n".join(blocks)

    def event_block(
        self,
        event: RecordedEvent,
        parent_start: int,
        indent_level: int,
    ) -> str:
        body = event.body
        is_exception = event.name == "exception"
        indent_str = "  " * indent_level
        cont = "  " * (indent_level + 1)

        rel = f"+{_fmt_duration(event.time_ns - parent_start)}"
        cw = self._width
        name_w = max(1, cw - indent_level * 2 - _TIME_COL - 1 - 2)
        ev_name = event.name
        if len(ev_name) > name_w:
            ev_name = ev_name[: max(0, name_w - 1)] + "…"
        name_part = ev_name.ljust(name_w) if not event.attributes else ev_name
        style = "red" if is_exception else "white"
        line = (
            indent_str
            + _styled(rel[:_TIME_COL].ljust(_TIME_COL), "dim")
            + " "
            + _styled(name_part, style)
        )
        if event.attributes:
            line += "  "
            for i, (k, v) in enumerate(event.attributes.items()):
                if i > 0:
                    line += " "
                line += _styled(f"{k}: ", "dim") + _styled(str(v), "white")
        line = self._fit_line(line)
        if body is None:
            return line

        parts: list[str] = [line]
        if is_exception:
            for ln in body.rstrip("\n").splitlines():
                parts.append(self._fit_line(cont + _styled(ln, "dim")))
        else:
            for ln in body.splitlines():
                parts.append(self._fit_line(cont + _styled(ln, "dim")))
        return "\n".join(parts)


class TTYTracer:
    """Live span tree on a TTY while `with tracer` is active.

    A background thread repaints open roots on a timer so in-flight attributes
    and events appear without hooking every mutation. `stats()` is always zero
    (dev-only sink).
    """

    __slots__ = (
        "_children",
        "_closed_blocks",
        "_live",
        "_lock",
        "_open_span_ids",
        "_out",
        "_rendered_width",
        "_roots",
        "_running",
        "_thread",
        "_write_lock",
    )

    def __init__(self, *, out: TextIO | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        self._write_lock = threading.Lock()
        self._roots: dict[UUID, RecordingSpan] = {}
        self._children: dict[UUID, list[RecordingSpan]] = {}
        self._open_span_ids: set[UUID] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._closed_blocks: list[str] = []
        self._live: _LiveRegion | None = None
        self._rendered_width: int | None = None

    def _terminal_width(self) -> int:
        try:
            return max(1, shutil.get_terminal_size((80, 24)).columns - 1)
        except OSError:
            return 79

    def __enter__(self) -> TTYTracer:
        self._start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._stop()

    def create(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> RecordingSpan:
        if not self._running:
            raise RuntimeError("TTYTracer must be entered before creating spans.")
        span_id = uuid7()
        if parent is None:
            trace_id = span_id
            parent_id = None
        else:
            trace_id = parent.trace_id
            parent_id = parent.id
        span = RecordingSpan(
            span_id,
            self,
            trace_id,
            parent_id,
            name,
            attributes=dict(attributes) if attributes else None,
        )
        with self._lock:
            if span.parent_id is None:
                self._roots[span.id] = span
                self._open_span_ids.add(span.id)
            else:
                if span.parent_id not in self._open_span_ids:
                    raise RuntimeError(
                        "Cannot create a child span for a span that is not open."
                    )
                self._children.setdefault(span.parent_id, []).append(span)
                self._open_span_ids.add(span.id)
        return span

    def on_end(self, span: Span) -> None:
        span = cast(RecordingSpan, span)
        with self._lock:
            self._open_span_ids.discard(span.id)
            if span.parent_id is None:
                width = self._terminal_width()
                self._closed_blocks.append(
                    TTYRenderer(width, self._children).root_block(span)
                )
                self._cleanup(span.id)

    def stats(self) -> TelemetryStats:
        return TelemetryStats()

    def _start(self) -> None:
        if self._out is sys.stdout or getattr(self._out, "isatty", lambda: False)():
            enable_vt_for_stream(self._out)
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.flush()

    def flush(self) -> None:
        with self._lock:
            width = self._terminal_width()
            self._rendered_width = width
            remaining = [span for span in self._roots.values() if span.started]
            renderer = TTYRenderer(width, self._children)
            # Roots that finished after the last repaint tick are still queued; drain them
            # too or they would be silently dropped on shutdown.
            blocks = self._closed_blocks + [
                renderer.root_block(span) for span in remaining
            ]
            self._closed_blocks = []
            self._roots.clear()
            self._children.clear()
            self._open_span_ids.clear()
        self._stop_live()
        self._print_blocks(blocks)

    def _should_render_locked(self) -> bool:
        if self._closed_blocks:
            return True
        current_width = self._terminal_width()
        if self._rendered_width is not None and current_width != self._rendered_width:
            return True
        if self._live is not None:
            return True
        return any(span.started and span.in_progress for span in self._roots.values())

    def _loop(self) -> None:
        # Timer repaints open roots so live attributes/events appear without mutation hooks.
        # Timer-driven so in-flight spans repaint even without new attribute mutations.
        while True:
            started_at = time.perf_counter()
            with self._lock:
                if not self._running:
                    return
                should_render = self._should_render_locked()
            if should_render:
                self._render()

            if (
                remaining := _REFRESH_INTERVAL_S - (time.perf_counter() - started_at)
            ) > 0:
                time.sleep(remaining)

    def _render(self) -> None:
        with self._lock:
            closed_blocks, live_text = self._render_plan_locked()
        self._write_render_plan(closed_blocks, live_text)

    def _render_plan_locked(self) -> tuple[list[str], str | None]:
        open_roots = [
            span for span in self._roots.values() if span.started and span.in_progress
        ]
        width = self._terminal_width()
        self._rendered_width = width
        renderer = TTYRenderer(width, self._children)

        closed_blocks = self._closed_blocks
        self._closed_blocks = []
        live_text = (
            renderer.live_text(open_roots)
            if open_roots or closed_blocks or self._live is not None
            else None
        )
        return closed_blocks, live_text

    def _write_render_plan(
        self, closed_blocks: list[str], live_text: str | None
    ) -> None:
        # Cursor must sit immediately after the previous live block when we erase.
        # So: erase live → print finished roots into scrollback → redraw live at the bottom.
        if self._live is not None:
            self._live.erase()
        self._print_blocks(closed_blocks)
        if live_text is None:
            return
        if self._live is None:
            self._live = _LiveRegion(self._out, self._write_lock)
        self._live.write(live_text)

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _print_blocks(self, blocks: list[str]) -> None:
        if not blocks:
            return
        with self._write_lock:
            self._out.write("\n".join(blocks))
            self._out.write("\n")
            self._out.flush()

    def _cleanup(self, span_id: UUID) -> None:
        self._open_span_ids.discard(span_id)
        self._roots.pop(span_id, None)
        for child in self._children.pop(span_id, []):
            self._cleanup(child.id)
