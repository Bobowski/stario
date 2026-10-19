"""TTY span tree for interactive development (stdlib + ANSI).

Not a production log format. Finished request roots scroll into history as a
ledger of completed traces; open roots repaint in a live footer at the bottom
of the terminal. Each frame is a single write wrapped in the terminal's
synchronized-output markers (``\\x1b[?2026h`` / ``\\x1b[?2026l``): erase the
previous live block, print newly finished roots, redraw the live block.

The erase is reflow-aware: it counts screen rows from the stored visible width
of every previous live line against the *current* raw terminal columns, so it
still reaches the first live row after a shrink and cannot touch scrollback.
The trailing ``\\x1b[J`` also clears a stray ``^C`` echo or a partial line left
on the cursor row.

While live and attached to ``sys.stdout``, ``sys.stdout`` and ``sys.stderr``
are replaced by a small pass-through proxy (``logging.StreamHandler`` streams
pointing at the originals are retargeted as well) so ``print`` and logging
interleave with the live region instead of corrupting it. Limits: raw file
descriptor writes, subprocess output, and handlers bound to other streams
bypass the proxy.

`TTYRenderer` is the pure span→string layer and the unit-test seam: no locks,
threads, or I/O.
"""

from __future__ import annotations

import codecs
import contextlib
import logging
import shutil
import sys
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from itertools import chain
from types import TracebackType
from typing import Any, TextIO, cast
from unicodedata import combining, east_asian_width
from uuid import UUID

from stario._terminal import RESET as _RESET
from stario._terminal import SGR, color_enabled, enable_vt_for_stream

from .core import Attributes, Span, TelemetryStats
from .spans import RecordedEvent, RecordedLink, RecordingSpan

_REFRESH_INTERVAL_S = 0.125
_ELLIPSIS = "…"
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAME_NS = 125_000_000
_NAME_MIN = 8
_GUTTER = 10
_MAX_BODY_LINES = 12
_MAX_VALUE_CHARS = 4000
_MAX_NESTED = 60
_NESTED_EDGE = 30
_PROMOTED_KEYS = (
    "request.method",
    "request.path",
    "response.status_code",
    "status_code",
)


def _terminal_size() -> tuple[int, int]:
    """Layout size (columns, rows); one-column margin so full-width lines do not wrap."""
    try:
        size = shutil.get_terminal_size((80, 24))
        return max(1, size.columns - 1), max(1, size.lines)
    except OSError:
        return 79, 24


def _terminal_columns() -> int:
    """Raw terminal columns (no margin); used by the reflow-aware erase."""
    try:
        return max(1, shutil.get_terminal_size((80, 24)).columns)
    except OSError:
        return 80


def _clock_ns() -> int:
    return time.time_ns()


def _isatty(stream: object) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except Exception:
        return False


def _ansi_sequence_end(text: str, index: int) -> int | None:
    if text[index : index + 2] != "\033[":
        return None
    for end in range(index + 2, len(text)):
        if "@" <= text[end] <= "~":
            return end + 1
    return None


def _cell_width(ch: str) -> int:
    if ch.isascii():
        return 1
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
    if ns < 0:
        ns = 0
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


def _sanitize(text: object, *, multiline: bool) -> str:
    """Make a user-supplied string terminal-safe.

    Tabs become four spaces, ``\\r`` variants become ``\\n``, newlines are kept
    only where multi-line is allowed (else ``↵``), and every remaining control
    character becomes a literal ``\\xNN`` escape so user text can never emit
    terminal escapes or break width accounting.
    """
    s = str(text).replace("\t", "    ").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in s:
        if ch == "\n":
            out.append("\n" if multiline else "↵")
        elif unicodedata.category(ch) == "Cc":
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


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
        except (ValueError, TypeError):
            continue
        return _status_color(code)
    return "green"


def _status_color(code: int) -> str:
    if 200 <= code < 300:
        return "green"
    if 300 <= code < 500:
        return "yellow"
    return "red"


def _promoted(span: RecordingSpan) -> tuple[str, dict[str, Any]]:
    """Display name and renderable attributes after request-name promotion."""
    attrs = dict(span.attributes or {})
    method = attrs.get("request.method")
    path = attrs.get("request.path")
    if method is not None and path is not None:
        for key in _PROMOTED_KEYS:
            attrs.pop(key, None)
        return f"{method} {path}", attrs
    return span.name, attrs


def _status_code(span: RecordingSpan) -> int | None:
    for key in ("response.status_code", "status_code"):
        if not span.attributes or key not in span.attributes:
            continue
        try:
            return int(span.attributes[key])
        except (ValueError, TypeError):
            continue
    return None


class _LiveRegion:
    """Bookkeeping for the live block currently on screen.

    Stores the text, layout width, and the visible width of every live line so
    the next frame can compute how many *screen rows* to walk back at whatever
    the terminal width is then (reflow-aware erase).
    """

    __slots__ = ("_line_widths", "_text", "_width")

    def __init__(self) -> None:
        self._text: str | None = None
        self._width: int | None = None
        self._line_widths: list[int] = []

    @property
    def has_content(self) -> bool:
        return self._text is not None

    def matches(self, text: str, width: int) -> bool:
        return self._text == text and self._width == width

    def store(self, text: str, width: int) -> None:
        if not text.endswith("\n"):
            text += "\n"
        self._text = text
        self._width = width
        self._line_widths = [_visible_width(line) for line in text.splitlines()]

    def clear(self) -> None:
        self._text = None
        self._width = None
        self._line_widths = []

    def erase_seq(self, columns: int) -> str:
        """Sequence that returns the cursor to the first live row and clears below.

        Empty when no live block is on screen, so callers never wipe the line
        the cursor happens to be on (e.g. a partial proxy line or ``^C``).
        """
        if not self._line_widths:
            return ""
        cols = max(1, columns)
        rows = sum(max(1, -(-w // cols)) for w in self._line_widths)
        self.clear()
        return "\r" + (f"\x1b[{rows}A" if rows else "") + "\x1b[J"


class _LiveEntry:
    """One candidate line of a live root block (header or attribute line)."""

    __slots__ = ("content", "kind", "order", "parent", "pri", "seq", "span", "waiting")

    def __init__(
        self,
        pri: int,
        seq: int,
        kind: str,
        span: RecordingSpan,
        parent: int,
        order: int,
        waiting: bool = False,
        content: str = "",
    ) -> None:
        self.pri = pri
        self.seq = seq
        self.kind = kind
        self.span = span
        self.parent = parent
        self.order = order
        self.waiting = waiting
        self.content = content


# (flow_text, flow_width, is_block, key, value_multiline, value_flat)
_FlowItem = tuple[str, int, bool, str, str, str]


class TTYRenderer:
    """Pure string rendering for span trees; no I/O, locks, or threads.

    Pass terminal width, a ``parent_id → children`` map, a fixed ``now_ns``
    (all open-span durations in one frame come from the same instant so a
    parent is never shorter than a child), and whether ANSI color is on.

    `history_block` renders one root as a ledger entry for scrollback;
    `live_text` renders the active path of open roots for the live footer.
    """

    __slots__ = ("_children", "_color", "_now", "_width")

    def __init__(
        self,
        width: int,
        children: dict[UUID, list[RecordingSpan]],
        *,
        now_ns: int,
        color: bool,
    ) -> None:
        self._width = max(1, width)
        self._children = children
        self._now = now_ns
        self._color = color

    # --------------------------------------------------------------
    # shared primitives

    def _styled(self, text: str, *styles: str) -> str:
        if not self._color or not text:
            return text
        codes = "".join(SGR[s] for s in styles if s in SGR)
        if not codes:
            return text
        return f"{codes}{text}{_RESET}"

    def _fit(self, line: str, width: int) -> str:
        return _clip_visible(line, width)

    def _duration_ns(self, span: RecordingSpan) -> int | None:
        if span.start_ns is None:
            return None
        if span.end_ns is not None:
            return span.end_ns - span.start_ns
        return self._now - span.start_ns

    def _open_children(self, span: RecordingSpan) -> list[RecordingSpan]:
        kids = [
            c
            for c in self._children.get(span.id, [])
            if c.start_ns is not None and c.in_progress
        ]
        kids.sort(key=lambda c: c.start_ns or 0)
        return kids

    def _glyph(self, span: RecordingSpan, *, orphan: bool, waiting: bool) -> str:
        if orphan:
            return self._styled("⚠", "yellow")
        if span.failed:
            return self._styled("✗", "red")
        if not span.in_progress:
            return self._styled("●", _span_status_style(span))
        if waiting:
            return self._styled("◌", "cyan", "dim")
        frame = (self._now // _FRAME_NS) % len(_SPINNER)
        return self._styled(_SPINNER[frame], "cyan")

    def _col_a(self, span: RecordingSpan, *, is_root: bool, root_start: int) -> str:
        if is_root:
            code = _status_code(span)
            if code is None:
                return " " * 9
            style = _status_color(code)
            # Yellow is pale on light themes; bold keeps 3xx/4xx readable.
            styles = ("yellow", "bold") if style == "yellow" else (style,)
            return self._styled(str(code).rjust(9), *styles)
        start = span.start_ns if span.start_ns is not None else root_start
        return self._styled(f"+{_fmt_duration(start - root_start)}".rjust(9), "dim")

    def _header_line(
        self,
        span: RecordingSpan,
        *,
        rail: str,
        width: int,
        is_root: bool,
        orphan: bool,
        waiting: bool,
        root_start: int,
        meta: str = "",
    ) -> str:
        if span.start_ns is None:
            raise RuntimeError("TTYRenderer can only render started spans")
        name, _ = _promoted(span)
        name_text = _sanitize(name, multiline=False)
        if is_root:
            name_text = self._styled(name_text, "bold")
        glyph = self._glyph(span, orphan=orphan, waiting=waiting)
        head_w = _visible_width(rail) + _visible_width(glyph) + 1
        id_tail = f" {_uuid_tail(span.id)}" if is_root and self._width >= 100 else ""

        col_a = self._col_a(span, is_root=is_root, root_start=root_start)
        dur = self._duration_ns(span)
        col_b = _fmt_duration(dur).rjust(9) if dur is not None else "—".rjust(9)
        open_m = self._styled(" ⋯", "dim") if span.in_progress else "  "

        # Narrowing: name needs >= 8 cells; drop colA first, then colB+open.
        id_w = _visible_width(id_tail)
        mode = 0
        name_w = width - head_w - (9 + 1 + 9 + 2 + id_w)
        if name_w < _NAME_MIN:
            mode = 1
            name_w = width - head_w - (9 + 2 + id_w)
        if name_w < _NAME_MIN:
            mode = 2
            name_w = width - head_w - id_w
        name_w = max(0, name_w)

        # The meta suffix is appended whole or not at all — never mid-token.
        meta_w = _visible_width(meta)
        if meta_w and _visible_width(name_text) + meta_w <= name_w:
            name_c = name_text + meta
        else:
            name_c = _clip_visible(name_text, name_w)
        gap = " " * max(0, name_w - _visible_width(name_c))
        line = self._styled(rail, "dim") + glyph + " " + name_c + gap
        if mode == 0:
            line += col_a + " " + col_b + open_m + self._styled(id_tail, "dim")
        elif mode == 1:
            line += col_b + open_m + self._styled(id_tail, "dim")
        else:
            line += self._styled(id_tail, "dim")
        return self._fit(line, width)

    # --------------------------------------------------------------
    # attribute flow layout

    def _flow_items(
        self,
        span: RecordingSpan,
        attrs: dict[str, Any],
        first_item: str | None,
    ) -> list[_FlowItem]:
        items: list[_FlowItem] = []
        if first_item is not None:
            text = self._styled(first_item, "dim")
            items.append((text, _visible_width(text), False, "", "", ""))
        fold = f"{span.name.split('.', 1)[0]}." if "." in span.name else None
        for raw_key in sorted(attrs):
            key = raw_key
            if fold and key.startswith(fold) and len(key) > len(fold):
                key = key[len(fold) :]
            key = _sanitize(key, multiline=False)
            raw = str(attrs[raw_key])[:_MAX_VALUE_CHARS]
            value_ml = _sanitize(raw, multiline=True)
            value_flat = _sanitize(raw, multiline=False)
            block = "\n" in value_ml
            text = self._styled(key, "dim") + " " + value_flat
            items.append((text, _visible_width(text), block, key, value_ml, value_flat))
        return items

    def _flow_lines(
        self, items: list[_FlowItem], avail: int, *, live: bool
    ) -> list[str]:
        avail = max(1, avail)
        sep = self._styled(" · ", "dim")
        lines: list[str] = []
        cur: list[str] = []
        cur_w = 0

        def flush() -> None:
            nonlocal cur, cur_w
            if cur:
                lines.append(sep.join(cur))
                cur = []
                cur_w = 0

        for text, vw, block, key, value_ml, value_flat in items:
            if block or vw > avail:
                flush()
                if live:
                    item_text = (
                        text
                        if not block
                        else (self._styled(key, "dim") + " " + value_flat)
                    )
                    lines.append(_clip_visible(item_text, avail))
                else:
                    lines.append(self._styled(key, "dim"))
                    wrapped = self._wrap_value(value_ml, max(1, avail - 2))
                    lines.extend("  " + w for w in wrapped[:_MAX_BODY_LINES])
                    if len(wrapped) > _MAX_BODY_LINES:
                        more = len(wrapped) - _MAX_BODY_LINES
                        lines.append("  " + self._styled(f"… {more} more lines", "dim"))
                continue
            if cur and cur_w + 3 + vw <= avail:
                cur.append(text)
                cur_w += 3 + vw
            else:
                flush()
                cur = [text]
                cur_w = vw
        flush()
        return lines

    def _wrap_value(self, value: str, width: int) -> list[str]:
        out: list[str] = []
        for para in value.split("\n"):
            s = para
            while _visible_width(s) > width:
                w = 0
                i = 0
                while i < len(s):
                    cw = _cell_width(s[i])
                    if w + cw > width:
                        break
                    w += cw
                    i += 1
                if i == 0:
                    i = 1
                cut = s.rfind(" ", 1, i + 1)
                if cut > 0:
                    out.append(s[:cut])
                    s = s[cut + 1 :]
                else:
                    out.append(s[:i])
                    s = s[i:]
            out.append(s)
        return out

    def _attr_lines(
        self,
        span: RecordingSpan,
        prefix: str,
        width: int,
        first_item: str | None,
    ) -> list[str]:
        _, attrs = _promoted(span)
        if not attrs and first_item is None:
            return []
        items = self._flow_items(span, attrs, first_item)
        avail = width - _visible_width(prefix)
        return [prefix + line for line in self._flow_lines(items, avail, live=False)]

    def _inline_attrs(self, attrs: dict[str, Any] | None) -> str:
        if not attrs:
            return ""
        parts = [
            self._styled(_sanitize(k, multiline=False), "dim")
            + " "
            + _sanitize(v, multiline=False)
            for k, v in sorted(attrs.items())
        ]
        return "  " + self._styled(" · ", "dim").join(parts)

    def _link_line(self, link: RecordedLink, prefix: str) -> str:
        text = f"↗ {_sanitize(link.name, multiline=False)} {_uuid_tail(link.span_id)}"
        if link.attributes:
            parts = [
                f"{_sanitize(k, multiline=False)} {_sanitize(v, multiline=False)}"
                for k, v in sorted(link.attributes.items())
            ]
            text += "  " + " · ".join(parts)
        return prefix + self._styled(text, "dim")

    # --------------------------------------------------------------
    # body blocks (errors, frames, event bodies)

    def _frame_lines(self, body: str, prefix: str, width: int) -> list[str]:
        """Framed traceback; long rows wrap (2-cell continuation indent), never clip."""
        text = _sanitize(body, multiline=True).rstrip("\n")
        # Budget after "{prefix}{edge} " minus the continuation indent.
        avail = max(1, width - _visible_width(prefix) - 4)
        rows: list[str] = []
        for line in text.split("\n"):
            wrapped = self._wrap_value(line, avail)
            rows.append(wrapped[0])
            rows.extend("  " + piece for piece in wrapped[1:])
        n = len(rows)
        out: list[str] = []
        for i, row in enumerate(rows):
            edge = "╭" if i == 0 else ("╰" if i == n - 1 else "│")
            out.append(prefix + self._styled(f"{edge} {row}", "dim"))
        return out

    def _body_lines(self, body: str, prefix: str, width: int) -> list[str]:
        """Event body; wraps instead of clipping, capped at _MAX_BODY_LINES wrapped rows."""
        text = _sanitize(body, multiline=True).rstrip("\n")
        avail = max(1, width - _visible_width(prefix))
        rows: list[str] = []
        for line in text.split("\n"):
            rows.extend(self._wrap_value(line, avail))
        out = [prefix + self._styled(row, "dim") for row in rows[:_MAX_BODY_LINES]]
        if len(rows) > _MAX_BODY_LINES:
            more = len(rows) - _MAX_BODY_LINES
            out.append(prefix + self._styled(f"… {more} more lines", "dim"))
        return out

    # --------------------------------------------------------------
    # history blocks

    def history_block(self, root: RecordingSpan) -> str:
        """Render one root (and its whole subtree) for scrollback."""
        if root.start_ns is None:
            raise RuntimeError("TTYRenderer can only render started spans")
        gutter = self._width >= 60
        inner = max(1, self._width - _GUTTER) if gutter else self._width
        first_item: str | None = None
        if not gutter:
            _, attrs = _promoted(root)
            if attrs:
                first_item = datetime.fromtimestamp(root.start_ns / 1e9).strftime(
                    "%H:%M:%S"
                )
        lines: list[str] = []
        self._emit_span(
            lines,
            root,
            anc="",
            connector="",
            is_last=True,
            is_root=True,
            inner=inner,
            root=root,
            first_item=first_item,
        )
        out: list[str] = []
        for i, line in enumerate(lines):
            line = self._fit(line, inner)
            if not gutter:
                out.append(line)
            elif i == 0:
                stamp = datetime.fromtimestamp(root.start_ns / 1e9).strftime("%H:%M:%S")
                out.append(self._styled(stamp, "dim") + "  " + line)
            else:
                out.append(" " * _GUTTER + line)
        return "\n".join(out)

    def _consumed_exception(self, span: RecordingSpan) -> RecordedEvent | None:
        """The exception event folded into the error line of a failed span."""
        if not span.failed:
            return None
        for event in span.events or []:
            if (
                event.name == "exception"
                and event.attributes
                and event.attributes.get("exc.message") == span.error
            ):
                return event
        return None

    def _nested_items(
        self, span: RecordingSpan, exclude: RecordedEvent | None
    ) -> list[tuple[int, RecordedEvent | RecordingSpan]]:
        return sorted(
            chain(
                ((e.time_ns, e) for e in span.events or [] if e is not exclude),
                (
                    (c.start_ns, c)
                    for c in self._children.get(span.id, [])
                    if c.start_ns is not None
                ),
            ),
            key=lambda x: x[0],
        )

    def _emit_span(
        self,
        lines: list[str],
        span: RecordingSpan,
        *,
        anc: str,
        connector: str,
        is_last: bool,
        is_root: bool,
        inner: int,
        root: RecordingSpan,
        first_item: str | None = None,
    ) -> None:
        rail = "" if is_root else anc + connector
        cont = "" if is_root else anc + ("  " if is_last else "│ ")
        root_start = root.start_ns or 0
        orphan = bool(root.end_ns) and span.in_progress
        waiting = any(c.in_progress for c in self._children.get(span.id, []))
        consumed = self._consumed_exception(span)
        nested = self._nested_items(span, consumed)
        lines.append(
            self._header_line(
                span,
                rail=rail,
                width=inner,
                is_root=is_root,
                orphan=orphan,
                waiting=waiting,
                root_start=root_start,
            )
        )
        body = cont + ("│" if nested else " ") + "  "
        if span.failed:
            error = span.error or "failed"
            exc_type = (
                consumed.attributes.get("exc.type")
                if consumed is not None and consumed.attributes
                else None
            )
            text = f"{exc_type}: {error}" if exc_type else error
            lines.append(body + self._styled(_sanitize(text, multiline=False), "red"))
            if consumed is not None and consumed.body:
                lines.extend(self._frame_lines(consumed.body, body, inner))
        if orphan:
            lines.append(
                body + self._styled("still open when the parent ended", "yellow")
            )
        lines.extend(self._attr_lines(span, body, inner, first_item))
        for link in span.links or []:
            lines.append(self._link_line(link, body))
        self._emit_nested(lines, nested, cont, inner, root)

    def _emit_nested(
        self,
        lines: list[str],
        items: list[tuple[int, RecordedEvent | RecordingSpan]],
        cont: str,
        inner: int,
        root: RecordingSpan,
    ) -> None:
        n = len(items)
        marker: str | None = None
        shown: list[tuple[int, RecordedEvent | RecordingSpan] | None]
        if n > _MAX_NESTED:
            mid = items[_NESTED_EDGE : n - _NESTED_EDGE]
            spans_n = sum(1 for _, it in mid if isinstance(it, RecordingSpan))
            events_n = len(mid) - spans_n
            marker = f"⋯ {len(mid)} more ({spans_n} spans, {events_n} events)"
            shown = [*items[:_NESTED_EDGE], None, *items[n - _NESTED_EDGE :]]
        else:
            shown = list(items)
        last = len(shown) - 1
        for i, entry in enumerate(shown):
            is_last = i == last
            connector = "└─" if is_last else "├─"
            if entry is None:
                lines.append(cont + "  " + self._styled(marker or "", "dim"))
                continue
            _, item = entry
            if isinstance(item, RecordedEvent):
                self._emit_event(
                    lines,
                    item,
                    cont=cont,
                    connector=connector,
                    is_last=is_last,
                    inner=inner,
                    root_start=root.start_ns or 0,
                )
            else:
                self._emit_span(
                    lines,
                    item,
                    anc=cont,
                    connector=connector,
                    is_last=is_last,
                    is_root=False,
                    inner=inner,
                    root=root,
                )

    def _emit_event(
        self,
        lines: list[str],
        event: RecordedEvent,
        *,
        cont: str,
        connector: str,
        is_last: bool,
        inner: int,
        root_start: int,
    ) -> None:
        rail = cont + connector
        body = cont + ("  " if is_last else "│ ") + "  "
        name = _sanitize(event.name, multiline=False)
        text = name
        if event.name == "exception":
            attrs = event.attributes or {}
            exc_type = attrs.get("exc.type")
            exc_msg = attrs.get("exc.message")
            if exc_type and exc_msg:
                label = f"{exc_type}: {exc_msg}"
            else:
                label = str(exc_msg or exc_type or "")
            if label:
                text += "  " + self._styled(_sanitize(label, multiline=False), "red")
            other = {
                k: v for k, v in attrs.items() if k not in ("exc.type", "exc.message")
            }
            text += self._inline_attrs(other)
        else:
            text += self._inline_attrs(event.attributes)
        offset = f"+{_fmt_duration(event.time_ns - root_start)}"
        lines.append(self._event_line(rail, text, offset, inner))
        if event.body:
            if event.name == "exception":
                lines.extend(self._frame_lines(event.body, body, inner))
            else:
                lines.extend(self._body_lines(event.body, body, inner))

    def _event_line(self, rail: str, text: str, offset: str, width: int) -> str:
        """`{rail}◇ {text}{gap}{offset}` with the offset parked in the colA column."""
        rail_w = _visible_width(rail)
        target = width - 12  # end column of the span colA field
        budget = target - 9 - rail_w - 2
        if budget >= 4:
            clipped = _clip_visible(text, budget)
            gap = " " * max(0, budget - _visible_width(clipped))
            line = (
                self._styled(rail, "dim")
                + self._styled("◇", "dim")
                + " "
                + clipped
                + gap
                + self._styled(offset.rjust(9), "dim")
            )
        else:
            line = (
                self._styled(rail, "dim")
                + self._styled("◇", "dim")
                + " "
                + _clip_visible(text, max(0, width - rail_w - 2))
            )
        return self._fit(line, width)

    # --------------------------------------------------------------
    # live area

    def live_text(self, open_roots: list[RecordingSpan], *, max_rows: int) -> str:
        roots = sorted(open_roots, key=lambda s: s.start_ns or 0)
        if not roots or max_rows <= 0:
            return ""
        newest_first = list(reversed(roots))
        if max_rows == 1:
            entries = [_LiveEntry(0, 0, "header", newest_first[0], -1, 0)]
            rendered = self._render_live_root(entries, {0}, newest_first[0])
            return rendered[0] if rendered else ""

        budget = max_rows - 1  # first row is the "─ N open" line
        shown: list[RecordingSpan] = []
        for span in newest_first:
            if len(shown) >= budget:
                break
            shown.append(span)
        shown.reverse()  # display oldest at top
        not_shown = len(roots) - len(shown)

        per_root = {id(r): self._live_root_entries(r) for r in shown}
        grants = {id(r): 1 for r in shown}
        rows_left = budget - len(shown)
        for span in newest_first:
            if rows_left <= 0:
                break
            entries = per_root.get(id(span))
            if entries is None:
                continue
            grant = min(len(entries) - 1, rows_left)
            grants[id(span)] += grant
            rows_left -= grant

        lines = [self._open_line(len(roots), not_shown)]
        for span in shown:
            entries = per_root[id(span)]
            ordered = sorted(entries, key=lambda e: (e.pri, e.seq))
            kept = {e.seq for e in ordered[: grants[id(span)]]}
            lines.extend(self._render_live_root(entries, kept, span))
        return "\n".join(self._fit(line, self._width) for line in lines)

    def _open_line(self, n_open: int, not_shown: int) -> str:
        text = f"─ {n_open} open"
        if not_shown:
            text += f" · {not_shown} not shown"
        pad = max(0, self._width - _visible_width(text) - 1)
        return self._fit(self._styled(text + " " + "─" * pad, "dim"), self._width)

    def _live_meta(self, span: RecordingSpan) -> str:
        done = failed = 0
        for child in self._children.get(span.id, []):
            if not child.finished:
                continue
            if child.failed:
                failed += 1
            else:
                done += 1
        meta = ""
        if done:
            meta += self._styled(f" · {done} done", "dim")
        if failed:
            meta += self._styled(f" · {failed} failed", "red")
        if span.events:
            meta += self._styled(f" · {len(span.events)} events", "dim")
        return meta

    def _live_root_entries(self, root: RecordingSpan) -> list[_LiveEntry]:
        """Candidate lines of one live root, tagged by display priority."""
        entries: list[_LiveEntry] = []

        def add(
            pri: int,
            kind: str,
            span: RecordingSpan,
            parent: int,
            order: int,
            waiting: bool = False,
            content: str = "",
        ) -> int:
            entries.append(
                _LiveEntry(
                    pri, len(entries), kind, span, parent, order, waiting, content
                )
            )
            return len(entries) - 1

        def attr_entries(span: RecordingSpan, owner: int, depth: int, pri: int) -> None:
            _, attrs = _promoted(span)
            if not attrs:
                return
            items = self._flow_items(span, attrs, None)
            # Prefix is owner.cont (2 cells/level) + 3 cells; content packs
            # against that width whichever rail survives the row budget.
            avail = self._width - 2 * depth - 3
            for line in self._flow_lines(items, avail, live=True):
                add(pri, "attr", span, owner, 0, content=line)

        root_idx = add(
            0, "header", root, -1, 0, waiting=bool(self._open_children(root))
        )
        attr_entries(root, root_idx, 0, 4)

        def walk(span: RecordingSpan, parent: int, depth: int) -> None:
            for i, child in enumerate(self._open_children(span)):
                kids = self._open_children(child)
                waiting = bool(kids)
                idx = add(
                    2 if waiting else 1, "header", child, parent, i, waiting=waiting
                )
                if not waiting:
                    attr_entries(child, idx, depth, 3)
                walk(child, idx, depth + 1)

        walk(root, root_idx, 1)
        return entries

    def _render_live_root(
        self,
        entries: list[_LiveEntry],
        kept: set[int],
        root: RecordingSpan,
    ) -> list[str]:
        """Emit kept entries in DFS order, rails recomputed over the kept set."""
        present: set[int] = set()
        for i in kept:
            j = i
            while j >= 0 and j not in present:
                present.add(j)
                j = entries[j].parent
        last_present: dict[int, int] = {}
        has_present_child: set[int] = set()
        for e in entries:
            if e.kind != "header" or e.parent < 0 or e.seq not in present:
                continue
            if e.order > last_present.get(e.parent, -1):
                last_present[e.parent] = e.order
            has_present_child.add(e.parent)

        lines: list[str] = []
        root_start = root.start_ns or 0
        for e in entries:
            if e.seq not in kept:
                continue
            if e.kind == "header":
                rail, _ = self._live_rail(e, entries, last_present)
                lines.append(
                    self._header_line(
                        e.span,
                        rail=rail,
                        width=self._width,
                        is_root=e.parent < 0,
                        orphan=False,
                        waiting=e.waiting,
                        root_start=root_start,
                        meta=self._live_meta(e.span),
                    )
                )
            else:
                owner = entries[e.parent]
                _, cont = self._live_rail(owner, entries, last_present)
                prefix = cont + ("│" if owner.seq in has_present_child else " ") + "  "
                lines.append(prefix + e.content)
        return lines

    def _live_rail(
        self,
        entry: _LiveEntry,
        entries: list[_LiveEntry],
        last_present: dict[int, int],
    ) -> tuple[str, str]:
        """(rail, continuation) for an entry, recomputed over present siblings."""
        if entry.parent < 0:
            return "", ""
        chain_: list[_LiveEntry] = []
        node = entry
        while node.parent >= 0:
            chain_.append(node)
            node = entries[node.parent]
        parts = [
            "  " if a.order == last_present.get(a.parent, -2) else "│ "
            for a in reversed(chain_[1:])
        ]
        ancestor = "".join(parts)
        is_last = entry.order == last_present.get(entry.parent, -2)
        rail = ancestor + ("└─" if is_last else "├─")
        cont = ancestor + ("  " if is_last else "│ ")
        return rail, cont


class _Passthrough:
    """``sys.stdout``/``sys.stderr`` proxy that yields the live block to plain prints.

    Complete lines are written through after erasing the live block; the next
    render tick repaints it. A ``flush()`` with a partial line (pdb/input
    prompt) pauses the render thread until a later newline write.
    """

    def __init__(
        self,
        original: TextIO,
        lock: threading.Lock,
        erase: Callable[[], None],
        set_paused: Callable[[bool], None],
    ) -> None:
        self._original = original
        self._lock = lock
        self._erase = erase
        self._set_paused = set_paused
        self._buf = ""
        self.encoding = getattr(original, "encoding", None)
        self.errors = getattr(original, "errors", None)

    @property
    def original(self) -> TextIO:
        return self._original

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            if "\n" in self._buf:
                cut = self._buf.rfind("\n") + 1
                complete, self._buf = self._buf[:cut], self._buf[cut:]
                self._erase()
                self._original.write(complete)
                self._original.flush()
                if not self._buf:
                    self._set_paused(False)
        return len(s)

    def writelines(self, lines: Iterable[str]) -> None:
        for s in lines:
            self.write(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                self._erase()
                self._original.write(self._buf)
                self._buf = ""
                self._original.flush()
                self._set_paused(True)
            else:
                self._original.flush()

    def drain(self) -> None:
        """Teardown: write a remaining partial line (plus newline) out of the way."""
        with self._lock:
            if self._buf:
                self._original.write(self._buf + "\n")
                self._buf = ""
            self._original.flush()

    def isatty(self) -> bool:
        return _isatty(self._original)

    def fileno(self) -> int:
        return self._original.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class TTYTracer:
    """Live span tree on a TTY while `with tracer` is active.

    A background thread repaints open roots every 125 ms; `on_end` of a root
    only detaches the subtree into a pending queue — the render thread (or
    `flush`) formats history off the request thread. Spans get
    ``span.mutex = self._lock`` so mutations serialize with rendering.
    `stats()` is always zero (dev-only sink).
    """

    __slots__ = (
        "_children",
        "_color",
        "_deferred",
        "_err_proxy",
        "_live_enabled",
        "_lock",
        "_open_span_ids",
        "_out",
        "_out_proxy",
        "_paused",
        "_pending",
        "_prompt_paused",
        "_region",
        "_retargeted",
        "_roots",
        "_running",
        "_thread",
        "_write_enc",
        "_write_lock",
    )

    def __init__(self, *, out: TextIO | None = None, live: bool | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        is_tty = _isatty(self._out)
        self._live_enabled = is_tty if live is None else bool(live)
        self._color = color_enabled() and is_tty
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._roots: dict[UUID, RecordingSpan] = {}
        self._children: dict[UUID, list[RecordingSpan]] = {}
        self._open_span_ids: set[UUID] = set()
        self._pending: deque[tuple[RecordingSpan, dict[UUID, list[RecordingSpan]]]] = (
            deque()
        )
        self._deferred: list[str] = []
        self._region = _LiveRegion()
        self._paused = False
        self._running = False
        self._thread: threading.Thread | None = None
        self._out_proxy: _Passthrough | None = None
        self._err_proxy: _Passthrough | None = None
        self._retargeted: list[tuple[logging.StreamHandler[TextIO], TextIO]] = []
        self._prompt_paused = False
        # Non-UTF output streams get lossy-encoded copies of every write so a
        # CJK glyph or box edge can never raise UnicodeEncodeError.
        try:
            resolved = codecs.lookup(
                getattr(self._out, "encoding", None) or "utf-8"
            ).name
        except (LookupError, TypeError):
            resolved = "utf-8"
        self._write_enc = None if resolved.startswith("utf") else resolved

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
        span = RecordingSpan.create(self, name, attributes, parent=parent)
        with self._lock:
            # Any span activity resumes a paused live region (e.g. after a
            # pdb prompt, where the newline echo bypasses the proxy).
            self._paused = False
            span.mutex = self._lock
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
            self._paused = False
            self._open_span_ids.discard(span.id)
            if span.parent_id is not None:
                return
            root = self._roots.pop(span.id, None)
            if root is None:
                return
            subtree: dict[UUID, list[RecordingSpan]] = {}
            self._detach_locked(span.id, subtree)
            self._pending.append((root, subtree))

    def stats(self) -> TelemetryStats:
        return TelemetryStats()

    def _detach_locked(
        self, span_id: UUID, out: dict[UUID, list[RecordingSpan]]
    ) -> None:
        children = self._children.pop(span_id, [])
        out[span_id] = children
        for child in children:
            self._open_span_ids.discard(child.id)
            self._detach_locked(child.id, out)

    # --------------------------------------------------------------
    # lifecycle

    def _start(self) -> None:
        if self._out is sys.stdout or _isatty(self._out):
            enable_vt_for_stream(self._out)
        with self._lock:
            if self._running:
                return
            self._running = True
        if self._live_enabled and self._out is sys.stdout:
            self._install_passthrough()
        with self._lock:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _stop(self) -> None:
        with self._lock:
            self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
            self._thread = None
        self.flush()
        for proxy in (self._out_proxy, self._err_proxy):
            if proxy is not None:
                proxy.drain()
        self._restore_streams()

    def flush(self) -> None:
        with self._lock:
            width, _ = _terminal_size()
            now = _clock_ns()
            blocks: list[str] = []
            while self._pending:
                root, kids = self._pending.popleft()
                try:
                    blocks.append(
                        TTYRenderer(
                            width, kids, now_ns=now, color=self._color
                        ).history_block(root)
                    )
                except Exception as exc:
                    blocks.append(self._fallback_block(root, width, exc))
            remaining = sorted(
                (s for s in self._roots.values() if s.started),
                key=lambda s: s.start_ns or 0,
            )
            renderer = TTYRenderer(width, self._children, now_ns=now, color=self._color)
            for span in remaining:
                try:
                    blocks.append(renderer.history_block(span))
                except Exception as exc:
                    blocks.append(self._fallback_block(span, width, exc))
            self._roots.clear()
            self._children.clear()
            self._open_span_ids.clear()
        self._write_frame(blocks, None, width, force=True)

    # --------------------------------------------------------------
    # render thread

    def _should_render_locked(self) -> bool:
        if self._paused:
            return False
        if self._pending or self._region.has_content:
            return True
        return any(span.started and span.in_progress for span in self._roots.values())

    def _loop(self) -> None:
        while True:
            started_at = time.perf_counter()
            with self._lock:
                if not self._running:
                    return
                should_render = self._should_render_locked()
            if should_render:
                with contextlib.suppress(Exception):
                    self._render()

            if (
                remaining := _REFRESH_INTERVAL_S - (time.perf_counter() - started_at)
            ) > 0:
                time.sleep(remaining)

    def _render(self) -> None:
        if self._paused:
            return
        with self._lock:
            blocks, live_text, width = self._render_plan_locked()
        self._write_frame(blocks, live_text, width)

    def _render_plan_locked(self) -> tuple[list[str], str | None, int]:
        width, rows = _terminal_size()
        now = _clock_ns()
        blocks: list[str] = []
        while self._pending:
            root, kids = self._pending.popleft()
            try:
                blocks.append(
                    TTYRenderer(
                        width, kids, now_ns=now, color=self._color
                    ).history_block(root)
                )
            except Exception as exc:
                blocks.append(self._fallback_block(root, width, exc))
        live_text: str | None = None
        if self._live_enabled:
            open_roots = sorted(
                (s for s in self._roots.values() if s.started and s.in_progress),
                key=lambda s: s.start_ns or 0,
            )
            if open_roots:
                try:
                    live_text = TTYRenderer(
                        width, self._children, now_ns=now, color=self._color
                    ).live_text(open_roots, max_rows=max(1, rows // 2))
                except Exception as exc:
                    live_text = _clip_visible(
                        f"live view failed: {type(exc).__name__}", width
                    )
        return blocks, live_text, width

    def _fallback_block(self, span: RecordingSpan, width: int, exc: Exception) -> str:
        """One-line substitute for a root whose render raised — never drop a trace."""
        line = _sanitize(span.name, multiline=False)
        line += f"  (render failed: {type(exc).__name__})"
        if width >= 60 and span.start_ns is not None:
            stamp = datetime.fromtimestamp(span.start_ns / 1e9).strftime("%H:%M:%S")
            if self._color:
                stamp = f"{SGR['dim']}{stamp}{_RESET}"
            line = stamp + "  " + line
        return _clip_visible(line, width)

    # --------------------------------------------------------------
    # writes (under _write_lock only; never takes _lock)

    def _write_frame(
        self,
        blocks: list[str],
        live_text: str | None,
        width: int,
        *,
        force: bool = False,
    ) -> None:
        with self._write_lock:
            if self._paused:
                if not force:
                    self._deferred.extend(blocks)
                    return
                self._paused = False
            if self._deferred:
                blocks = self._deferred + blocks
                self._deferred = []
            if not self._live_enabled:
                if blocks:
                    self._out.write(self._to_out("\n".join(blocks) + "\n"))
                    self._out.flush()
                return
            live_text = live_text or ""
            if live_text and not live_text.endswith("\n"):
                live_text += "\n"
            if not blocks and self._region.matches(live_text, width):
                return
            if not blocks and not live_text and not self._region.has_content:
                return
            parts = ["\x1b[?2026h"]
            if self._prompt_paused:
                # Resumed after a partial-line prompt (pdb/input): step onto a
                # fresh line so nothing lands next to the prompt text.
                parts.append("\n")
                self._prompt_paused = False
            parts.append(self._region.erase_seq(_terminal_columns()))
            if blocks:
                parts.append("\n".join(blocks) + "\n")
            if live_text:
                parts.append(live_text)
            parts.append("\x1b[?2026l")
            self._out.write(self._to_out("".join(parts)))
            self._out.flush()
            if live_text:
                self._region.store(live_text, width)

    def _to_out(self, text: str) -> str:
        """Round-trip through the output encoding when it cannot hold text."""
        enc = self._write_enc
        if enc is None:
            return text
        return text.encode(enc, "replace").decode(enc)

    # --------------------------------------------------------------
    # stdout/stderr pass-through

    def _erase_live(self) -> None:
        """Erase the on-screen live block via ``_out`` (the TTY stream).

        Proxies call this before writing their own text so cursor escapes
        never land in a redirected stderr.
        """
        seq = self._region.erase_seq(_terminal_columns())
        if seq:
            self._out.write(self._to_out(seq))
            self._out.flush()

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        # Remember pauses caused by a partial-line flush: the newline that ends
        # the prompt is echoed by the terminal, not written through the proxy,
        # so the next resumed frame must start on a fresh line. Cleared by a
        # proxy newline write or once the resume frame has been emitted.
        self._prompt_paused = paused

    def _install_passthrough(self) -> None:
        orig_out = self._out
        orig_err = sys.stderr
        out_proxy = _Passthrough(
            orig_out, self._write_lock, self._erase_live, self._set_paused
        )
        self._out_proxy = out_proxy
        sys.stdout = cast(TextIO, out_proxy)
        proxies: dict[int, _Passthrough] = {id(orig_out): out_proxy}
        # A redirected stderr must stay untouched and escape-free.
        if _isatty(orig_err):
            err_proxy = _Passthrough(
                orig_err, self._write_lock, self._erase_live, self._set_paused
            )
            self._err_proxy = err_proxy
            sys.stderr = cast(TextIO, err_proxy)
            proxies[id(orig_err)] = err_proxy
        loggers: list[logging.Logger] = [logging.root]
        loggers += [
            logger
            for logger in logging.Logger.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        ]
        for logger in loggers:
            for handler in logger.handlers:
                if not isinstance(handler, logging.StreamHandler):
                    continue
                stream_handler = cast("logging.StreamHandler[TextIO]", handler)
                proxy = proxies.get(id(stream_handler.stream))
                if proxy is not None:
                    self._retargeted.append((stream_handler, stream_handler.stream))
                    stream_handler.setStream(cast(TextIO, proxy))

    def _restore_streams(self) -> None:
        for handler, stream in self._retargeted:
            with contextlib.suppress(Exception):
                handler.setStream(stream)
        self._retargeted.clear()
        if self._out_proxy is not None and sys.stdout is self._out_proxy:
            sys.stdout = self._out_proxy.original
        if self._err_proxy is not None and sys.stderr is self._err_proxy:
            sys.stderr = self._err_proxy.original
        self._out_proxy = None
        self._err_proxy = None
