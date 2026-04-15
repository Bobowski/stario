"""TTY span tree output (stdlib + ANSI); interactive dev UX, not a production log format.

Layout: ``_LiveRegion`` keeps an in-place footer (cursor-up + clear); ``_render_locked`` erases
it, prints finished roots to scrollback, then redraws open roots so the cursor always sits right
after the live block (required for the next erase).
"""

import os
import shutil
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain
from typing import Any
from uuid import UUID, uuid7

from stario.console import enable_windows_console_vt

from .core import Span
from .tracebacks import format_exception_for_telemetry

_REFRESH_INTERVAL_S = 0.125
_TIME_COL = 14
# Single-cell live placeholder so ``splitlines()`` still counts as one row.
_IDLE_LIVE = " "

_RESET = "\033[0m"
# --- ANSI (respects NO_COLOR in ``_styled`` only; live cursor escapes are unconditional) ---

_STYLES: dict[str, str] = {
    "dim": "\033[2m",
    "white": "\033[37m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


def _styled(text: str, style: str) -> str:
    if os.environ.get("NO_COLOR", "").strip():
        return text
    p = _STYLES.get(style)
    if not p:
        return text
    return f"{p}{text}{_RESET}"


@dataclass(slots=True)
class _SpanState:
    id: UUID
    name: str
    parent_id: UUID | None
    start_ns: int
    end_ns: int | None = None
    error: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    events: list["_SpanEvent"] = field(default_factory=list)
    links: list["_SpanLink"] = field(default_factory=list)

    @property
    def started(self) -> bool:
        return self.start_ns != 0

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


@dataclass(slots=True)
class _SpanEvent:
    time_ns: int
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    body: Any | None = None


@dataclass(slots=True, frozen=True)
class _SpanLink:
    span_id: UUID
    attributes: dict[str, Any] = field(default_factory=dict)


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


def _span_status_style(span: _SpanState) -> str:
    if span.in_progress:
        return "cyan"
    if span.failed:
        return "red"
    for key in ("response.status_code", "status_code"):
        if key not in span.attrs:
            continue
        try:
            code = int(span.attrs[key])
        except (ValueError, TypeError):
            continue
        if 200 <= code < 300:
            return "green"
        if 300 <= code < 500:
            return "yellow"
        return "red"
    return "green"


def _build_status_trailer(span: _SpanState, max_len: int) -> str:
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

    __slots__ = ("_out", "_lock", "_lines")

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
            self._out.write(content)
            if not content.endswith("\n"):
                self._out.write("\n")
            self._out.flush()
            # ``" ".splitlines()`` is [] — still one screen row for the idle placeholder.
            self._lines = max(1, len(content.splitlines())) if content else 1

    def _clear_written_lines(self) -> None:
        for _ in range(self._lines):
            self._out.write("\x1b[1A\x1b[2K\r")

    def stop(self) -> None:
        with self._lock:
            self._erase_unlocked()


class TTYTracer:
    """Live span tree on a TTY (dev UX); background thread repaints while ``with tracer`` is active."""

    __slots__ = (
        "_spans",
        "_roots",
        "_children",
        "_lock",
        "_write_lock",
        "_thread",
        "_running",
        "_dirty",
        "_live",
        "_out",
    )

    def __init__(self) -> None:
        self._out = sys.stdout
        self._write_lock = threading.Lock()
        self._spans: dict[UUID, _SpanState] = {}
        self._roots: dict[UUID, _SpanState] = {}
        self._children: dict[UUID, list[_SpanState]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._dirty = False
        self._live: _LiveRegion | None = None

    @property
    def _width(self) -> int:
        try:
            return max(40, shutil.get_terminal_size((80, 24)).columns)
        except OSError:
            return 80

    def __enter__(self) -> TTYTracer:
        self._start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop()

    def create(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        /,
        *,
        parent_id: UUID | None = None,
    ) -> Span:
        if not self._running:
            self._start()
        span = Span(id=uuid7(), tracer=self)
        state = _SpanState(
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
        /,
        *,
        body: Any | None = None,
    ) -> None:
        event = _SpanEvent(
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
        /,
    ) -> None:
        link = _SpanLink(
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
        enable_windows_console_vt()
        with self._lock:
            if self._running:
                return
            self._running = True
            self._dirty = True
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
            self._stop_live_locked()
            remaining = [span for span in self._roots.values() if span.started]
            self._print_roots_locked(remaining)
            self._roots.clear()
            self._children.clear()
            self._spans.clear()
            self._dirty = False

    def _loop(self) -> None:
        # Timer-driven so in-flight spans repaint even without new attribute mutations.
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
        with self._lock:
            self._render_locked()

    def _render_locked(self) -> None:
        closed_roots = [
            span for span in self._roots.values() if span.started and span.finished
        ]
        open_roots = [
            span for span in self._roots.values() if span.started and span.in_progress
        ]

        # Cursor must sit immediately after the previous live block when we erase.
        # So: erase live → print finished roots into scrollback → redraw live at the bottom.
        if self._live is not None:
            self._live.erase()

        if closed_roots:
            closed_blocks = [
                (span, self._span_tree_str(span, parent_start_ns=None, indent_level=0))
                for span in closed_roots
            ]
            self._print_roots_locked(closed_roots, blocks=closed_blocks)

        if open_roots or closed_roots or self._live is not None:
            text = self._live_renderable_str(open_roots)
            if self._live is None:
                self._live = _LiveRegion(self._out, self._write_lock)
            self._live.write(text)

        self._dirty = False

    def _live_renderable_str(self, open_roots: list[_SpanState]) -> str:
        if not open_roots:
            return _IDLE_LIVE
        parts: list[str] = []
        for span in open_roots:
            parts.append(self._root_separator_line_str(span))
            parts.append(self._span_tree_str(span, parent_start_ns=None, indent_level=0))
        return "\n".join(parts)

    def _stop_live_locked(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _println(self, s: str = "") -> None:
        with self._write_lock:
            self._out.write(s + "\n")
            self._out.flush()

    def _print_roots_locked(
        self,
        spans: list[_SpanState],
        *,
        blocks: list[tuple[_SpanState, str]] | None = None,
    ) -> None:
        block_map = {span.id: block for span, block in blocks or []}
        for span in spans:
            self._println(self._root_separator_line_str(span))
            self._println(
                block_map.get(span.id)
                or self._span_tree_str(span, parent_start_ns=None, indent_level=0)
            )
            self._cleanup(span.id)

    def _cleanup(self, span_id: UUID) -> None:
        self._roots.pop(span_id, None)
        self._spans.pop(span_id, None)
        for child in self._children.pop(span_id, []):
            self._cleanup(child.id)

    def _root_separator_line_str(self, span: _SpanState) -> str:
        w = max(16, self._width)
        style = _span_status_style(span)
        return _styled("─" * w, style)

    def _span_header_line_str(
        self,
        span: _SpanState,
        *,
        parent_start_ns: int | None,
        indent_level: int,
    ) -> str:
        indent = "  " * indent_level
        tw = max(40, self._width)
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

        max_trailer = tw - prefix_len - 5
        trailer = _build_status_trailer(span, max(8, max_trailer))
        name_w = max(4, tw - prefix_len - len(trailer) - 1)
        name = span.name
        if len(name) > name_w:
            name = name[: max(0, name_w - 1)] + "…"

        gap = tw - prefix_len - len(name) - len(trailer)
        attempts = 0
        while gap < 0 and attempts < 6:
            attempts += 1
            max_trailer = max(8, max_trailer - 8)
            trailer = _build_status_trailer(span, max_trailer)
            name_w = max(4, tw - prefix_len - len(trailer) - 1)
            name = span.name
            if len(name) > name_w:
                name = name[: max(0, name_w - 1)] + "…"
            gap = tw - prefix_len - len(name) - len(trailer)
        if gap < 0:
            name = "…"
            gap = max(0, tw - prefix_len - len(name) - len(trailer))

        return (
            _styled(indent + time_s + " ", "dim")
            + _styled(name, "white")
            + _styled(" " * gap, "dim")
            + _styled(trailer, style)
        )

    def _render_attribute_lines_str(
        self, attrs: Mapping[str, Any], base_level: int
    ) -> list[str]:
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
            lines.append(line)
        return lines

    def _render_link_lines_str(self, links: list[_SpanLink], base_level: int) -> list[str]:
        base = "  " * base_level
        lines: list[str] = []
        for link in links:
            tail = _uuid_tail(link.span_id)
            parts = base + _styled("link ", "dim") + _styled(tail, "white")
            if link.attributes:
                parts += "  "
                for i, (k, v) in enumerate(link.attributes.items()):
                    if i:
                        parts += " "
                    parts += _styled(f"{k}=", "dim") + _styled(str(v), "white")
            lines.append(parts)
        return lines

    def _span_tree_str(
        self,
        span: _SpanState,
        *,
        parent_start_ns: int | None,
        indent_level: int,
    ) -> str:
        parts: list[str] = [
            self._span_header_line_str(
                span, parent_start_ns=parent_start_ns, indent_level=indent_level
            )
        ]
        attrs = span.attrs
        if attrs:
            parts.extend(self._render_attribute_lines_str(attrs, indent_level + 1))
        if span.links:
            parts.extend(self._render_link_lines_str(span.links, indent_level + 1))
        nested = self._nested_items_str(span, indent_level)
        if nested:
            parts.append(nested)
        return "\n".join(parts)

    def _nested_items_str(self, span: _SpanState, indent_level: int) -> str | None:
        items = sorted(
            chain(
                ((e.time_ns, e) for e in span.events),
                (
                    (c.start_ns, c)
                    for c in self._children.get(span.id, [])
                    if c.started
                ),
            ),
            key=lambda x: x[0],
        )
        if not items:
            return None
        child_level = indent_level + 1
        blocks: list[str] = []
        for _, item in items:
            if isinstance(item, _SpanEvent):
                blocks.append(
                    self._event_block_str(item, span.start_ns, indent_level=child_level)
                )
            else:
                blocks.append(
                    self._span_tree_str(
                        item, parent_start_ns=span.start_ns, indent_level=child_level
                    )
                )
        return "\n".join(blocks)

    def _event_block_str(
        self,
        event: _SpanEvent,
        parent_start: int,
        indent_level: int,
    ) -> str:
        body = event.body
        is_exception = isinstance(body, BaseException)
        indent_str = "  " * indent_level
        cont = "  " * (indent_level + 1)

        rel = f"+{_fmt_duration(event.time_ns - parent_start)}"
        cw = max(40, self._width)
        name_w = max(4, cw - indent_level * 2 - _TIME_COL - 1 - 2)
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
        if body is None:
            return line

        parts: list[str] = [line]
        if is_exception:
            if isinstance(body, BaseException):
                text = format_exception_for_telemetry(body).rstrip("\n")
                for ln in text.splitlines():
                    parts.append(cont + _styled(ln, "dim"))
        else:
            body_str = str(body) if not isinstance(body, str) else body
            for ln in body_str.splitlines():
                parts.append(cont + _styled(ln, "dim"))
        return "\n".join(parts)
