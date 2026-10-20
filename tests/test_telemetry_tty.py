"""TTY tracer (Ledger) tests.

Formatting is verified through `TTYRenderer` on hand-built `RecordingSpan`
records with a fixed `now_ns`. Tracer lifecycle and live I/O drive
`TTYTracer._render()` with `_running=True` and monkeypatched terminal helpers
(`_terminal_size`, `_terminal_columns`, `_clock_ns`) so the refresh thread is
not part of the assertions.
"""

import io
import logging
import re
import sys
import threading
import time
from typing import TextIO
from uuid import UUID, uuid4

from stario.telemetry.noop import NoOpTracer
from stario.telemetry.spans import RecordedEvent, RecordedLink, RecordingSpan
from stario.telemetry.tty import (
    TTYRenderer,
    TTYTracer,
    _clip_visible,
    _LiveRegion,
    _visible_width,
)

_NOOP = NoOpTracer()

# Fixed wall-clock anchor so rendered times are stable: 2023-11-14T22:13:20Z.
_T0 = 1_700_000_000 * 1_000_000_000
_NOW = _T0 + 10_000_000
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FORBIDDEN_SGR = re.compile(r"\x1b\[(?:[34]0|37|9\d|4\d|10\d|38;)")
_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def make_span(
    name: str = "request",
    *,
    parent: RecordingSpan | None = None,
    start_ns: int = _T0,
    end_ns: int | None = _T0 + 5_000_000,
    error: str | None = None,
    attributes: dict | None = None,
    events: list[RecordedEvent] | None = None,
    links: list[RecordedLink] | None = None,
) -> RecordingSpan:
    span_id = uuid4()
    return RecordingSpan(
        id=span_id,
        tracer=_NOOP,
        trace_id=span_id if parent is None else parent.trace_id,
        parent_id=None if parent is None else parent.id,
        name=name,
        start_ns=start_ns,
        end_ns=end_ns,
        error=error,
        attributes=attributes,
        events=events,
        links=links,
    )


def render(
    span: RecordingSpan,
    *,
    children: dict[UUID, list[RecordingSpan]] | None = None,
    width: int = 120,
    now: int = _NOW,
    color: bool = False,
) -> str:
    return TTYRenderer(width, children or {}, now_ns=now, color=color).history_block(
        span
    )


def _complex_tree() -> tuple[RecordingSpan, dict[UUID, list[RecordingSpan]]]:
    root = make_span(
        "request",
        attributes={
            "request.method": "GET",
            "request.path": "/projects/42",
            "user.id": 17,
            "session": "a81f09c2",
            "response.status_code": 200,
        },
        end_ns=_T0 + 48_200_000,
    )
    db = make_span(
        "db.query",
        parent=root,
        start_ns=_T0 + 1_200_000,
        end_ns=_T0 + 13_200_000,
        attributes={
            "db.statement": "SELECT * FROM projects WHERE id = ?",
            "db.rows": 1,
        },
        events=[
            RecordedEvent(
                time_ns=_T0 + 9_800_000,
                name="cache.miss",
                attributes={"key": "project:42"},
            )
        ],
    )
    render_span = make_span(
        "render",
        parent=root,
        start_ns=_T0 + 14_000_000,
        end_ns=_T0 + 47_100_000,
        attributes={"template": "project.html"},
    )
    fetch = make_span(
        "fetch avatar",
        parent=render_span,
        start_ns=_T0 + 20_000_000,
        end_ns=_T0 + 44_500_000,
        attributes={"url": "https://cdn.example/a/17.png", "note": "東京"},
    )
    return root, {root.id: [db, render_span], render_span.id: [fetch]}


class TestSpanStates:
    def test_running_leaf_shows_spinner_glyph(self):
        span = make_span("request", end_ns=None)
        line = render(span, width=90)

        frame = (_NOW // 125_000_000) % 10
        assert _SPINNER[frame] in line

    def test_spinner_frame_depends_on_now(self):
        span = make_span("request", end_ns=None)
        a = render(span, width=90, now=_NOW)
        b = render(span, width=90, now=_NOW + 125_000_000)
        assert _SPINNER[0] in a
        assert _SPINNER[1] in b

    def test_open_parent_with_open_child_is_waiting(self):
        root = make_span("request", end_ns=None)
        child = make_span(
            "db.query", parent=root, start_ns=_T0 + 2_000_000, end_ns=None
        )
        text = render(root, children={root.id: [child]}, width=90)

        root_line, child_line = text.splitlines()
        assert "◌" in root_line
        assert any(g in child_line for g in _SPINNER)

    def test_done_and_failed_glyphs(self):
        done = make_span("request")
        failed = make_span("request", error="boom")
        assert "●" in render(done, width=90)
        assert "✗" in render(failed, width=90)

    def test_open_parent_duration_covers_child(self):
        root = make_span("request", end_ns=None)
        child = make_span(
            "db.query", parent=root, start_ns=_T0 + 2_000_000, end_ns=None
        )
        text = render(root, children={root.id: [child]}, width=90, now=_T0 + 10_000_000)

        lines = text.splitlines()
        assert "10 ms" in lines[0]
        assert "8.0 ms" in lines[1]

    def test_orphan_when_root_ends_before_child(self):
        root = make_span("request", end_ns=_T0 + 10_000_000)
        child = make_span(
            "db.query", parent=root, start_ns=_T0 + 2_000_000, end_ns=None
        )
        text = render(root, children={root.id: [child]}, width=90)

        assert "⚠" in text
        assert "still open when the parent ended" in text


class TestHeaders:
    def test_name_promotion_removes_promoted_keys(self):
        root = make_span(
            "request",
            attributes={
                "request.method": "GET",
                "request.path": "/projects/42",
                "response.status_code": 200,
                "user.id": 17,
            },
        )
        text = render(root, width=90)

        assert "GET /projects/42" in text
        assert "200" in text
        assert "request.method" not in text
        assert "request.path" not in text
        assert "response.status_code" not in text
        assert "user.id" in text

    def test_status_code_in_col_a_and_offsets_from_root(self):
        root, children = _complex_tree()
        text = render(root, children=children, width=90)

        root_line = text.splitlines()[0]
        assert "200" in root_line
        db_line = next(ln for ln in text.splitlines() if "db.query" in ln)
        assert "+1.2 ms" in db_line
        fetch_line = next(ln for ln in text.splitlines() if "fetch avatar" in ln)
        # Offset is from the ROOT start, not the parent.
        assert "+20 ms" in fetch_line

    def test_root_id_tail_at_width_100(self):
        span = make_span("request")
        wide = render(span, width=110)
        assert str(span.id)[-8:] in wide
        narrow = render(span, width=90)
        assert str(span.id)[-8:] not in narrow

    def test_bare_root_is_one_line(self):
        span = make_span("request")
        assert "\n" not in render(span, width=90)
        assert "\n" not in render(span, width=40)


class TestHistoryGutter:
    def test_gutter_time_at_wide_widths(self):
        root, children = _complex_tree()
        lines = render(root, children=children, width=90).splitlines()

        assert re.match(r"^\d{2}:\d{2}:\d{2}  ", lines[0])
        for line in lines[1:]:
            assert line.startswith(" " * 10)

    def test_no_gutter_below_60_and_time_joins_flow(self):
        root = make_span(
            "request",
            attributes={"request.method": "GET", "request.path": "/x", "user.id": 3},
        )
        text = render(root, width=50)

        first = text.splitlines()[0]
        assert not re.match(r"^\d{2}:\d{2}:\d{2}", first)
        # The time lands in the attribute flow when the root has attributes.
        assert re.search(r"\d{2}:\d{2}:\d{2} ·", text)

    def test_no_gutter_and_no_attrs_stays_one_line(self):
        root = make_span("request")
        text = render(root, width=50)
        assert "\n" not in text
        assert not re.search(r"\d{2}:\d{2}:\d{2}", text)


class TestAttributeFlow:
    def test_short_items_pack_on_one_line(self):
        root = make_span(
            "request",
            attributes={"aa": "1", "bb": "2", "cc": "3"},
        )
        text = render(root, width=90)
        assert "aa 1 · bb 2 · cc 3" in text

    def test_long_value_wraps_without_losing_info(self):
        value = " ".join(f"word{i:02d}" for i in range(40))
        root = make_span("request", attributes={"payload": value})
        text = render(root, width=50)

        assert "payload" in text
        assert "more lines" not in text
        joined = " ".join(text.split())
        for i in range(40):
            assert f"word{i:02d}" in joined

    def test_very_long_value_caps_at_twelve_lines(self):
        value = " ".join(f"word{i:02d}" for i in range(400))
        root = make_span("request", attributes={"payload": value})
        text = render(root, width=40)
        assert "more lines" in text

    def test_multiline_value_becomes_block(self):
        root = make_span("request", attributes={"payload": "first\nsecond\nthird"})
        text = render(root, width=90)
        assert "first" in text
        assert "second" in text
        assert "third" in text

    def test_key_folding_uses_name_prefix(self):
        root = make_span(
            "server.startup",
            attributes={
                "server.host": "0.0.0.0",
                "server.port": 8000,
                "limits.max_request_header_bytes": 65536,
            },
        )
        text = render(root, width=90)
        assert "host 0.0.0.0" in text
        assert "port 8000" in text
        assert "limits.max_request_header_bytes 65536" in text


class TestEventsErrorsLinks:
    def test_event_line_and_body(self):
        root = make_span(
            "request",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000_000,
                    name="cache.miss",
                    attributes={"key": "p:42"},
                    body="lookup failed\ndetail",
                )
            ],
        )
        text = render(root, width=90)
        assert "◇" in text
        assert "cache.miss" in text
        assert "key p:42" in text
        assert "lookup failed" in text

    def test_failed_span_shows_exc_type_and_frame(self):
        root = make_span(
            "request",
            error="bad password hash",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 3_000_000,
                    name="exception",
                    attributes={
                        "exc.type": "ValueError",
                        "exc.message": "bad password hash",
                    },
                    body='Traceback (most recent call last):\n  File "a.py", line 1\nValueError: bad password hash',
                )
            ],
        )
        text = render(root, width=90)

        assert "✗" in text
        assert "ValueError: bad password hash" in text
        assert "╭ " in text
        assert "╰ " in text
        assert "◇ exception" not in text

    def test_frames_and_bodies_wrap_instead_of_clipping(self):
        body = "Traceback (most recent call last):\n" + "x" * 200 + "\nValueError: boom"
        root = make_span(
            "worker",
            error="boom",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000,
                    name="exception",
                    attributes={"exc.type": "ValueError", "exc.message": "boom"},
                    body=body,
                ),
                RecordedEvent(time_ns=_T0 + 2_000, name="note", body="y" * 200),
            ],
        )
        text = render(root, width=60)
        assert "…" not in text
        for line in text.splitlines():
            assert _visible_width(line) <= 60
        # Every character of both long rows survived the wrap.
        assert text.count("x") == 200
        assert text.count("y") == 200

    def test_exception_event_on_unfailed_span_shows_line_and_frame(self):
        root = make_span(
            "request",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000_000,
                    name="exception",
                    attributes={"exc.type": "ValueError", "exc.message": "hmm"},
                    body="Traceback:\nline",
                )
            ],
        )
        text = render(root, width=90)
        assert "◇ exception" in text
        assert "ValueError: hmm" in text
        assert "╭ " in text

    def test_links_render_dim_arrow_lines(self):
        root = make_span(
            "request",
            links=[RecordedLink("parent.trace", uuid4(), {"kind": "retry"})],
        )
        text = render(root, width=90)
        assert "↗ parent.trace" in text
        assert "kind retry" in text

    def test_nested_collapse_marker_over_60_items(self):
        root = make_span(
            "request",
            events=[
                RecordedEvent(time_ns=_T0 + i * 1_000, name=f"e{i}") for i in range(70)
            ],
        )
        text = render(root, width=90)
        assert "⋯ 10 more (0 spans, 10 events)" in text

    def test_sanitizing_blocks_escapes_and_controls(self):
        root = make_span(
            "req\nuest",
            attributes={"payload": "a\x1b[31mb\tc", "multi": "x\ny"},
        )
        text = render(root, width=90)
        assert "\x1b" not in text
        assert "\\x1b" in text
        assert "↵" in text
        for line in text.splitlines():
            assert _visible_width(line) <= 90


class TestColorRules:
    def test_no_forbidden_sgr_and_styles_closed(self):
        root, children = _complex_tree()
        open_root = make_span(
            "request",
            end_ns=None,
            attributes={"request.method": "GET", "request.path": "/x"},
        )
        for text in (
            render(root, children=children, width=90, color=True),
            TTYRenderer(90, children, now_ns=_NOW, color=True).live_text(
                [open_root], max_rows=12
            ),
        ):
            assert not _FORBIDDEN_SGR.search(text)
            for line in text.splitlines():
                assert _visible_width(line) <= 90
                open_style = False
                for match in _SGR.finditer(line):
                    open_style = match.group(1) != "0"
                assert not open_style, line

    def test_yellow_status_is_bolded(self):
        root = make_span(
            "request",
            attributes={"response.status_code": 302},
        )
        text = render(root, width=90, color=True)
        assert "\x1b[33m\x1b[1m      302" in text


class TestWidthSweep:
    def test_every_line_fits_every_width(self):
        root, children = _complex_tree()
        open_root, open_children = _complex_tree()
        open_root.end_ns = None
        for width in range(1, 131):
            for color in (False, True):
                history = TTYRenderer(
                    width, children, now_ns=_NOW, color=color
                ).history_block(root)
                for line in history.splitlines():
                    assert _visible_width(line) <= width
                live = TTYRenderer(
                    width, open_children, now_ns=_NOW, color=color
                ).live_text([open_root], max_rows=12)
                for line in live.splitlines():
                    assert _visible_width(line) <= width


class TestLiveArea:
    def _open_tree(self):
        root = make_span(
            "request",
            end_ns=None,
            attributes={"request.method": "GET", "request.path": "/x"},
        )
        done = make_span(
            "db.query", parent=root, start_ns=_T0 + 1_000_000, end_ns=_T0 + 4_000_000
        )
        failed = make_span(
            "cache.put",
            parent=root,
            start_ns=_T0 + 4_000_000,
            end_ns=_T0 + 6_000_000,
            error="nope",
        )
        open_child = make_span(
            "render", parent=root, start_ns=_T0 + 6_000_000, end_ns=None
        )
        root.events = [RecordedEvent(time_ns=_T0 + 2_000_000, name="ev")]
        kids = {root.id: [done, failed, open_child]}
        return root, kids

    def test_only_active_path_shown_with_meta(self):
        root, kids = self._open_tree()
        text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=12
        )
        assert "render" in text
        assert "db.query" not in text
        assert "cache.put" not in text
        assert "1 done" in text
        assert "1 failed" in text
        assert "events" in text

    def test_max_rows_respected(self):
        root, kids = self._open_tree()
        for rows in (1, 2, 5, 12):
            text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
                [root], max_rows=rows
            )
            assert len(text.splitlines()) <= rows

    def test_oldest_roots_dropped_first(self):
        roots = [
            make_span(f"r{i}", start_ns=_T0 + i * 1_000_000, end_ns=None)
            for i in range(4)
        ]
        text = TTYRenderer(90, {}, now_ns=_NOW, color=False).live_text(
            roots, max_rows=4
        )
        assert "3 not shown" in text or "not shown" in text
        assert "r3" in text
        assert "r0" not in text

    def test_budget_one_shows_newest_header_only(self):
        roots = [
            make_span(f"r{i}", start_ns=_T0 + i * 1_000_000, end_ns=None)
            for i in range(3)
        ]
        text = TTYRenderer(90, {}, now_ns=_NOW, color=False).live_text(
            roots, max_rows=1
        )
        lines = text.splitlines()
        assert len(lines) == 1
        assert "r2" in lines[0]
        assert "open" not in lines[0]

    def test_root_glyph_is_waiting_while_a_child_is_open(self):
        root, kids = self._open_tree()
        text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=12
        )
        root_line = next(ln for ln in text.splitlines() if "GET /x" in ln)
        leaf_line = next(ln for ln in text.splitlines() if "render" in ln)
        assert "◌" in root_line
        assert any(g in leaf_line for g in _SPINNER)

        solo = make_span("alone", end_ns=None)
        alone = TTYRenderer(90, {}, now_ns=_NOW, color=False).live_text(
            [solo], max_rows=12
        )
        assert any(g in alone for g in _SPINNER)
        assert "◌" not in alone

    def test_meta_never_clips_mid_token(self):
        root, kids = self._open_tree()
        meta = " · 1 done · 1 failed · 1 events"
        for width in range(20, 60):
            text = TTYRenderer(width, kids, now_ns=_NOW, color=False).live_text(
                [root], max_rows=12
            )
            root_line = text.splitlines()[1]
            # Meta is appended whole or dropped entirely.
            assert ("·" in root_line) == (meta in root_line), root_line

    def test_running_leaf_attributes_and_root_attributes_last(self):
        root = make_span("request", end_ns=None, attributes={"root.attr": 1})
        child = make_span(
            "db.query",
            parent=root,
            start_ns=_T0 + 1_000_000,
            end_ns=None,
            attributes={"sql.rows": 5},
        )
        kids = {root.id: [child]}
        text = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=12
        )
        assert "sql.rows" in text
        assert "root.attr" in text
        # With a tight budget the running leaf keeps its attrs before the root's.
        tight = TTYRenderer(90, kids, now_ns=_NOW, color=False).live_text(
            [root], max_rows=4
        )
        assert "sql.rows" in tight
        assert "root.attr" not in tight


class _CountingIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def write(self, s: str) -> int:
        self.writes += 1
        return super().write(s)


class _TermPatch:
    def __init__(self, columns: int, lines: int, now: int) -> None:
        self.columns = columns
        self.lines = lines
        self.now = now

    def size(self) -> tuple[int, int]:
        return max(1, self.columns - 1), max(1, self.lines)

    def cols(self) -> int:
        return self.columns

    def clock(self) -> int:
        return self.now


def _patch_terminal(monkeypatch, *, columns=80, lines=24, now=_NOW) -> _TermPatch:
    import stario.telemetry.tty as tty_module

    term = _TermPatch(columns, lines, now)
    monkeypatch.setattr(tty_module, "_terminal_size", term.size)
    monkeypatch.setattr(tty_module, "_terminal_columns", term.cols)
    monkeypatch.setattr(tty_module, "_clock_ns", term.clock)
    return term


def _manual_tracer(out: TextIO, *, live=True) -> TTYTracer:
    tracer = TTYTracer(out=out, live=live)
    tracer._running = True
    return tracer


def _open_root(tracer: TTYTracer, name="request") -> RecordingSpan:
    root = tracer.create(name)
    root.start()
    root.start_ns = _T0
    return root


class TestTracerIO:
    def test_unchanged_live_frame_writes_nothing(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = _CountingIO()
        tracer = _manual_tracer(out)
        _open_root(tracer)

        tracer._render()
        first = out.writes

        tracer._render()
        assert out.writes == first

    def test_frame_is_one_write_wrapped_in_sync_markers(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = _CountingIO()
        tracer = _manual_tracer(out)
        _open_root(tracer)

        tracer._render()
        text = out.getvalue()
        assert out.writes == 1
        assert text.startswith("\x1b[?2026h")
        assert text.endswith("\x1b[?2026l")

    def test_history_and_live_still_one_write(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = _CountingIO()
        tracer = _manual_tracer(out)
        done = _open_root(tracer, "done.request")
        done.end()
        _open_root(tracer, "keep.request")

        tracer._render()
        text = out.getvalue()
        assert out.writes == 1
        assert "done.request" in text
        assert "keep.request" in text

    def test_erase_reflow_aware_after_column_shrink(self, monkeypatch):
        term = _patch_terminal(monkeypatch, columns=80)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer)
        root.attr("pad", "x" * 120)
        root.attr("pad2", "y" * 120)

        tracer._render()
        live_lines = tracer._region._line_widths
        assert live_lines
        assert all(w == 79 for w in live_lines)

        term.columns = 40
        root.attr("pad3", "z" * 5)
        tracer._render()

        expected_rows = sum(-(-w // 40) for w in live_lines)
        assert f"\x1b[{expected_rows}A" in out.getvalue()
        assert expected_rows == 2 * len(live_lines)

    def test_erase_starts_with_cr_and_clears_down(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer)
        tracer._render()

        root.attr("k", "v")
        tracer._render()
        assert "\r" in out.getvalue()
        assert "\x1b[J" in out.getvalue()

    def test_live_false_never_writes_escapes(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out, live=False)
        root = _open_root(tracer)
        tracer._render()
        root.end()
        tracer._render()

        text = out.getvalue()
        assert "request" in text
        assert "\x1b[" not in text

    def test_root_end_formats_lazily_on_render_thread(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        calls = 0
        original = TTYRenderer.history_block

        def counting(self, root):
            nonlocal calls
            calls += 1
            return original(self, root)

        monkeypatch.setattr(TTYRenderer, "history_block", counting)
        root = _open_root(tracer)
        root.end()
        assert calls == 0

        tracer._render()
        assert calls == 1
        assert "request" in out.getvalue()

    def test_flush_is_safe_to_call_twice(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer)
        root.end()
        tracer.flush()
        tracer.flush()
        assert "request" in out.getvalue()

    def test_non_utf_stream_gets_replaced_text(self, monkeypatch):
        _patch_terminal(monkeypatch)
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding="ascii")
        tracer = _manual_tracer(out, live=False)
        root = _open_root(tracer)
        root.end()

        tracer._render()  # must not raise UnicodeEncodeError
        out.flush()
        data = buf.getvalue().decode("ascii")
        assert "?" in data  # the ● glyph lossy-encoded
        assert "request" in data

    def test_history_render_failure_emits_fallback(self, monkeypatch):
        class _BadStr:
            def __str__(self) -> str:
                raise RuntimeError("unprintable")

        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out, live=False)
        root = _open_root(tracer, "broken.request")
        root.attr("bad", _BadStr())
        root.end()

        tracer._render()
        text = out.getvalue()
        assert "broken.request" in text
        assert "render failed: RuntimeError" in text

    def test_live_render_failure_emits_fallback_line(self, monkeypatch):
        class _BadStr:
            def __str__(self) -> str:
                raise RuntimeError("unprintable")

        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        root = _open_root(tracer, "open.request")
        root.attr("bad", _BadStr())

        tracer._render()
        assert "live view failed: RuntimeError" in out.getvalue()

    def test_context_manager_flushes_open_roots(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        with TTYTracer(out=out, live=True) as tracer:
            root = tracer.create("request.in-flight", {"http.method": "GET"})
            root.start()
            child = tracer.create("db.query", parent=root)
            child.start()
            child.end()

        text = out.getvalue()
        assert "request.in-flight" in text
        assert "db.query" in text
        assert "http.method" in text


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestPassthrough:
    def _proxy_tracer(self, monkeypatch) -> tuple[TTYTracer, _FakeTTY]:
        _patch_terminal(monkeypatch)
        fake = _FakeTTY()
        monkeypatch.setattr(sys, "stdout", fake)
        tracer = TTYTracer()
        tracer._running = True
        tracer._install_passthrough()
        return tracer, fake

    def test_print_yields_live_block_and_repaints(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            assert tracer._region.has_content

            print("hello")
            text = fake.getvalue()
            assert "\x1b[J" in text
            assert text.index("\x1b[J") < text.index("hello\n")

            tracer._render()
            assert tracer._region.has_content
            text = tracer._region._text
            assert text is not None
            assert "request" in text
        finally:
            tracer._restore_streams()

    def test_partial_write_pauses_and_newline_resumes(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            root = _open_root(tracer)
            tracer._render()

            sys.stdout.write("prompt> ")
            sys.stdout.flush()
            assert tracer._paused
            fake.seek(0)
            fake.truncate()
            tracer._render()
            assert fake.getvalue() == ""

            sys.stdout.write("answer\n")
            assert not tracer._paused
            tracer._render()
            assert "request" in fake.getvalue()
            root.end()
        finally:
            tracer._restore_streams()

    def test_streams_restored(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        assert isinstance(sys.stdout, type(tracer._out_proxy))
        tracer._restore_streams()
        assert sys.stdout is fake
        assert sys.stderr is not tracer._err_proxy

    def test_stderr_not_proxied_when_not_a_tty(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out_tty = _FakeTTY()
        err_file = io.StringIO()  # redirected stderr: isatty() is False
        monkeypatch.setattr(sys, "stdout", out_tty)
        monkeypatch.setattr(sys, "stderr", err_file)

        tracer = TTYTracer()
        tracer._running = True
        tracer._install_passthrough()
        try:
            assert sys.stderr is err_file
            _open_root(tracer)
            tracer._render()
            print("oops", file=sys.stderr)
            assert "oops" in err_file.getvalue()
            assert "\x1b[" not in err_file.getvalue()
        finally:
            tracer._restore_streams()

    def test_stderr_tty_text_erases_via_stdout(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out_tty = _FakeTTY()
        err_tty = _FakeTTY()
        monkeypatch.setattr(sys, "stdout", out_tty)
        monkeypatch.setattr(sys, "stderr", err_tty)

        tracer = TTYTracer()
        tracer._running = True
        tracer._install_passthrough()
        try:
            assert sys.stderr is not err_tty
            _open_root(tracer)
            tracer._render()
            print("oops", file=sys.stderr)
            # The erase went to the tracer's out; the text to stderr.
            assert "\x1b[J" in out_tty.getvalue()
            assert "oops" in err_tty.getvalue()
            assert "\x1b[" not in err_tty.getvalue()
        finally:
            tracer._restore_streams()

    def test_writelines_goes_through_the_proxy_buffer(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            fake.seek(0)
            fake.truncate()

            sys.stdout.writelines(["one\n", "two\n"])
            assert "one\ntwo\n" in fake.getvalue()
            assert "\x1b[J" in fake.getvalue()
        finally:
            tracer._restore_streams()

    def test_resume_frame_starts_on_a_fresh_line(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            sys.stdout.write("prompt> ")
            sys.stdout.flush()
            assert tracer._paused

            fake.seek(0)
            fake.truncate()
            span = tracer.create("after.pause")
            span.start()
            span.end()
            tracer._render()
            # The newline keeps live/history off the prompt line.
            assert fake.getvalue().startswith("\x1b[?2026h\n")
        finally:
            tracer._restore_streams()

    def test_span_activity_resumes_paused_rendering(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        try:
            _open_root(tracer)
            tracer._render()
            # Partial line + flush: pauses (pdb prompt scenario).
            sys.stdout.write("prompt> ")
            sys.stdout.flush()
            assert tracer._paused

            fake.seek(0)
            fake.truncate()
            tracer._render()
            assert fake.getvalue() == ""

            # Any span activity resumes; a root end queues history.
            span = tracer.create("after.pause")
            assert not tracer._paused
            span.start()
            span.end()
            tracer._render()
            assert "after.pause" in fake.getvalue()
        finally:
            tracer._restore_streams()

    def test_logging_handler_retargeted_and_restored(self, monkeypatch):
        tracer, fake = self._proxy_tracer(monkeypatch)
        logger = logging.getLogger("stario.test.tty.passthrough")
        handler = logging.StreamHandler(fake)
        logger.addHandler(handler)
        try:
            tracer._restore_streams()
            tracer._install_passthrough()
            assert handler.stream is tracer._out_proxy
            tracer._restore_streams()
            assert handler.stream is fake
        finally:
            tracer._restore_streams()
            logger.removeHandler(handler)


class TestThreadSafety:
    def test_concurrent_mutation_and_render(self, monkeypatch):
        _patch_terminal(monkeypatch)
        out = io.StringIO()
        tracer = _manual_tracer(out)
        errors: list[BaseException] = []
        stop = threading.Event()

        def hammer(i: int) -> None:
            try:
                while not stop.is_set():
                    root = tracer.create(f"worker.{i}")
                    root.start()
                    root.attr("k", "v")
                    root.event("tick", {"i": i})
                    child = root.step("db.query")
                    child.start()
                    child.end()
                    root.end()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            tracer._render()
        stop.set()
        for t in threads:
            t.join()

        assert not errors
        assert "worker.0" in out.getvalue()


class TestPerformance:
    def test_live_frame_with_many_roots_is_fast(self):
        children: dict[UUID, list[RecordingSpan]] = {}
        roots = []
        for i in range(50):
            root = make_span(f"request.{i}", start_ns=_T0 - i * 1_000_000, end_ns=None)
            kids = [
                make_span(
                    f"child.{i}.{j}",
                    parent=root,
                    start_ns=_T0 + j * 100,
                    end_ns=None,
                )
                for j in range(20)
            ]
            children[root.id] = kids
            roots.append(root)

        renderer = TTYRenderer(120, children, now_ns=_NOW, color=False)
        start = time.perf_counter()
        renderer.live_text(roots, max_rows=24)
        assert time.perf_counter() - start < 2.0

    def test_history_block_with_two_thousand_items_is_fast(self):
        root = make_span("request", end_ns=_T0 + 100_000_000)
        kids = [
            make_span(f"child.{i}", parent=root, start_ns=_T0 + i, end_ns=_T0 + i + 1)
            for i in range(1000)
        ]
        root.events = [
            RecordedEvent(time_ns=_T0 + i, name=f"e{i}") for i in range(1000)
        ]
        renderer = TTYRenderer(120, {root.id: kids}, now_ns=_NOW, color=False)
        start = time.perf_counter()
        renderer.history_block(root)
        assert time.perf_counter() - start < 2.0


class TestLiveRegion:
    def test_erase_counts_rows_from_stored_widths(self):
        region = _LiveRegion()
        region.store("a" * 70 + "\n" + "b" * 70 + "\n", 79)
        seq = region.erase_seq(80)
        assert seq == "\r\x1b[2A\x1b[J"
        assert not region.has_content

        region.store("c" * 70 + "\n" + "d" * 70 + "\n", 79)
        seq = region.erase_seq(40)
        assert seq == "\r\x1b[4A\x1b[J"

        # An empty region never wipes the line the cursor is on.
        seq = region.erase_seq(40)
        assert seq == ""

    def test_matches_requires_same_text_and_width(self):
        region = _LiveRegion()
        region.store("hello\n", 80)
        assert region.matches("hello\n", 80)
        assert not region.matches("hello\n", 40)
        assert not region.matches("other\n", 80)


class TestHelpers:
    def test_visible_width_fullwidth(self):
        assert _visible_width("中") == 2
        assert _visible_width("東京") == 4
        assert _visible_width("é") == 1

    def test_clip_respects_cell_budget(self):
        clipped = _clip_visible("東京" * 10, 7)
        assert _visible_width(clipped) <= 7
        assert clipped.endswith("…")
