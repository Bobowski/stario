"""TTY tracer tests.

Formatting is verified through `TTYRenderer` on hand-built `RecordingSpan` records.
Tracer lifecycle is verified through the public context-manager API with an injected `out`
stream. Live I/O (skip-unchanged, height cap, resize) drives `TTYTracer._render`
so the refresh thread is not part of the assertions.
"""

import io
import threading
from uuid import UUID, uuid4, uuid7

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

# Fixed wall-clock anchor so rendered absolute times are stable: 2023-11-14T22:13:20Z.
_T0 = 1_700_000_000 * 1_000_000_000


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
    span_id = uuid7()
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
) -> str:
    return TTYRenderer(width, children or {}).root_block(span)


class TestTTYRendererHeaders:
    def test_finished_span_shows_duration_and_id_tail(self):
        span = make_span("request", end_ns=_T0 + 5_000_000)

        text = render(span)

        assert "request" in text
        assert "5.0 ms" in text
        assert str(span.id)[-8:] in text

    def test_in_progress_span_shows_ellipsis_instead_of_duration(self):
        span = make_span("request", end_ns=None)

        assert "…" in render(span)

    def test_failed_span_renders_error_in_trailer(self):
        span = make_span("request", error="db unavailable")

        assert "[db unavailable]" in render(span)

    def test_narrow_width_truncates_without_wrapping(self):
        target = uuid4()
        span = make_span(
            "request." + "x" * 80,
            attributes={
                "request.path": "/" + "deeply-nested/" * 12,
                "response.status_code": 200,
            },
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000_000,
                    name="event." + "y" * 80,
                    attributes={"payload": "z" * 80},
                    body="body line " + "w" * 80,
                )
            ],
            links=[RecordedLink("related." + "l" * 80, target, {"kind": "test"})],
        )

        text = render(span, width=20)

        assert "…" in text


class TestTTYRendererNesting:
    def test_child_spans_render_after_parent_with_relative_offset(self):
        parent = make_span("parent.request", end_ns=_T0 + 10_000_000)
        child = make_span(
            "child.step",
            parent=parent,
            start_ns=_T0 + 2_000_000,
            end_ns=_T0 + 3_000_000,
        )

        text = render(parent, children={parent.id: [child]})

        assert text.index("parent.request") < text.index("child.step")
        child_line = next(ln for ln in text.splitlines() if "child.step" in ln)
        assert "+2.0 ms" in child_line


class TestTTYRendererLiveHeight:
    def test_100_line_trace_keeps_header_and_newest_lines(self):
        span = _tall_span(attr_count=100)
        text = TTYRenderer(80, {}).live_text([span], max_rows=22)
        lines = text.splitlines()

        assert len(lines) == 22
        assert "request" in lines[1]
        assert "lines omitted" in text
        assert "key.000" not in text
        assert "key.099" in lines[-1]
        for line in lines:
            assert _visible_width(line) <= 80

    def test_short_tree_is_not_truncated(self):
        span = make_span("request", end_ns=None, attributes={"http.method": "GET"})
        text = TTYRenderer(80, {}).live_text([span], max_rows=22)

        assert "lines omitted" not in text
        assert "http.method" in text

    def test_exact_fit_has_no_omission_marker(self):
        span = _tall_span(attr_count=4)
        text = TTYRenderer(80, {}).live_text([span], max_rows=6)

        assert len(text.splitlines()) == 6
        assert "omitted" not in text
        assert "key.000" in text
        assert "key.003" in text

    def test_tiny_budgets_prefer_header_then_omission(self):
        span = _tall_span(attr_count=20)
        renderer = TTYRenderer(80, {})

        one = renderer.live_text([span], max_rows=1).splitlines()
        assert len(one) == 1
        assert "omitted" not in one[0]

        two = renderer.live_text([span], max_rows=2).splitlines()
        assert len(two) == 2
        assert "request" in two[1]
        assert "omitted" not in "\n".join(two)

        three = renderer.live_text([span], max_rows=3).splitlines()
        assert len(three) == 3
        assert "request" in three[1]
        assert "omitted" in three[2]

    def test_each_open_root_keeps_its_header(self):
        first = make_span(
            "alpha.request",
            end_ns=None,
            attributes={f"a.{i:02d}": i for i in range(40)},
        )
        second = make_span(
            "beta.request",
            end_ns=None,
            attributes={f"b.{i:02d}": i for i in range(40)},
        )

        text = TTYRenderer(80, {}).live_text([first, second], max_rows=22)
        lines = text.splitlines()

        assert len(lines) <= 22
        assert "alpha.request" in text
        assert "beta.request" in text
        assert text.index("alpha.request") < text.index("beta.request")
        assert "omitted" in text


class TestVisibleWidth:
    def test_fullwidth_characters_count_as_two_cells(self):
        assert _visible_width("中") == 2
        assert _visible_width("東京") == 4
        assert _visible_width("e\u0301") == 1

    def test_clip_preserves_cell_budget_for_fullwidth_text(self):
        clipped = _clip_visible("東京" * 10, 7)
        assert _visible_width(clipped) <= 7
        assert clipped.endswith("…")

    def test_rendered_lines_stay_within_width_for_fullwidth_names(self):
        span = make_span("東京" * 20, end_ns=_T0 + 1_000_000)
        text = render(span, width=20)

        for line in text.splitlines():
            assert _visible_width(line) <= 20

    def test_event_name_clips_by_visible_width(self):
        span = make_span(
            "request",
            events=[
                RecordedEvent(
                    time_ns=_T0 + 1_000_000,
                    name="東京" * 20,
                    attributes=None,
                    body=None,
                )
            ],
        )
        text = render(span, width=20)

        for line in text.splitlines():
            assert _visible_width(line) <= 20


class TestTracerPublicLifecycle:
    """Exercises `TTYTracer` only through its public surface (`out=`, `with`, spans)."""

    def test_context_manager_flushes_open_spans_on_exit(self, monkeypatch):
        _patch_terminal_size(monkeypatch)
        output = io.StringIO()

        with TTYTracer(out=output) as tracer:
            root = tracer.create("request.in-flight", {"http.method": "GET"})
            root.start()
            child = tracer.create("db.query", parent=root)
            child.start()
            child.end()
            # Root is intentionally left open: exit must flush it to scrollback.

        text = output.getvalue()
        assert "request.in-flight" in text
        assert "db.query" in text
        assert "http.method" in text


class TestLiveRegionIO:
    def test_unchanged_live_output_skips_terminal_writes(self, monkeypatch):
        _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = _CountingIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        root.attr("http.method", "GET")

        tracer._render()
        writes_after_first = output.writes
        first = output.getvalue()

        tracer._render()

        assert output.writes == writes_after_first
        assert output.getvalue() == first
        assert "\x1b[1A" not in first

    def test_100_line_live_region_stays_within_terminal_height(self, monkeypatch):
        _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = io.StringIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        for index in range(100):
            root.attr(f"key.{index:03d}", index)

        tracer._render()

        assert tracer._live is not None
        assert tracer._live._lines <= 22
        text = output.getvalue()
        assert "lines omitted" in text
        assert "key.099" in text
        assert text.count("\n") <= 22

    def test_terminal_resize_redraws_and_refits(self, monkeypatch):
        size = _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = _CountingIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        for index in range(100):
            root.attr(f"key.{index:03d}", index)

        tracer._render()
        first_writes = output.writes
        assert tracer._live is not None
        assert tracer._live._lines == 22

        size.columns = 40
        size.lines = 10
        tracer._render()

        assert output.writes > first_writes
        assert output.getvalue().count("\x1b[1A") == 22
        assert tracer._live._lines <= 8
        live_lines = output.getvalue().split("\x1b[1A\x1b[2K\r")[-1].splitlines()
        for line in live_lines:
            assert _visible_width(line) <= 39

    def test_changing_line_counts_erase_the_previous_block(self, monkeypatch):
        _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = io.StringIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        for index in range(8):
            root.attr(f"early.{index:02d}", index)

        tracer._render()
        assert tracer._live is not None
        first_lines = tracer._live._lines
        assert first_lines == 10  # separator + header + 8 attrs

        for index in range(8, 16):
            root.attr(f"late.{index:02d}", index)
        tracer._render()

        assert output.getvalue().count("\x1b[1A") == first_lines
        assert tracer._live._lines == 18  # separator + header + 16 attrs

        # A new, shorter root replaces the tall one so the live block shrinks.
        root.end()
        short = tracer.create("short.request")
        short.start()
        short.attr("http.method", "GET")
        tracer._render()

        assert tracer._live._lines == 3
        assert output.getvalue().count("\x1b[1A") == first_lines + 18

    def test_closed_root_rewrites_unchanged_live_sibling(self, monkeypatch):
        _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = _CountingIO()
        tracer = _manual_tracer(output)
        keep = tracer.create("keep.request")
        keep.start()
        keep.attr("http.method", "GET")
        tracer._render()
        assert tracer._live is not None
        live_before = tracer._live._text
        writes_after_first = output.writes

        done = tracer.create("done.request")
        done.start()
        done.end()
        tracer._render()

        assert output.writes > writes_after_first
        text = output.getvalue()
        assert "done.request" in text
        assert tracer._live._text == live_before
        assert "keep.request" in (tracer._live._text or "")
        assert "done.request" not in (tracer._live._text or "")

    def test_height_only_shrink_recaps_live_region(self, monkeypatch):
        size = _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = _CountingIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        for index in range(100):
            root.attr(f"key.{index:03d}", index)

        tracer._render()
        first_writes = output.writes
        assert tracer._live is not None
        assert tracer._live._lines == 22

        size.lines = 10
        tracer._render()

        assert output.writes > first_writes
        assert tracer._live._lines <= 8
        assert "lines omitted" in output.getvalue()

    def test_height_only_grow_on_short_tree_skips_writes(self, monkeypatch):
        size = _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = _CountingIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        root.attr("http.method", "GET")

        tracer._render()
        writes_after_first = output.writes

        size.lines = 40
        tracer._render()

        assert output.writes == writes_after_first

    def test_last_root_end_drops_live_region(self, monkeypatch):
        _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = io.StringIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        tracer._render()
        assert tracer._live is not None

        root.end()
        tracer._render()

        assert tracer._live is None
        assert "request" in output.getvalue()

    def test_child_end_stays_in_live_tree(self, monkeypatch):
        _patch_terminal_size(monkeypatch, columns=80, lines=24)
        output = io.StringIO()
        tracer = _manual_tracer(output)
        root = tracer.create("request")
        root.start()
        child = tracer.create("db.query", parent=root)
        child.start()

        tracer._render()
        child.end()
        tracer._render()

        text = output.getvalue()
        assert "request" in text
        assert "db.query" in text
        assert tracer._live is not None
        assert "db.query" in (tracer._live._text or "")


class TestLiveRegionLineAccounting:
    def test_erase_uses_previous_line_count(self):
        output = io.StringIO()
        region = _LiveRegion(output, threading.Lock())

        region.write("a\nb\nc\nd\ne", width=80)
        assert region._lines == 5
        output.seek(0)
        output.truncate()

        region.erase()
        assert output.getvalue().count("\x1b[1A") == 5
        assert region._lines == 0

        region.write("x\ny", width=80)
        assert region._lines == 2
        assert not region.matches("a\nb\nc\nd\ne", 80)
        assert region.matches("x\ny", 80)

    def test_matches_requires_same_width(self):
        output = io.StringIO()
        region = _LiveRegion(output, threading.Lock())
        region.write("hello", width=80)

        assert region.matches("hello", 80)
        assert not region.matches("hello", 40)


class _CountingIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def write(self, s: str) -> int:
        self.writes += 1
        return super().write(s)


class _TerminalSize:
    def __init__(self, columns: int, lines: int) -> None:
        self.columns = columns
        self.lines = lines

    def __call__(self) -> tuple[int, int]:
        return max(1, self.columns - 1), max(1, self.lines)


def _tall_span(*, attr_count: int) -> RecordingSpan:
    attributes = {f"key.{index:03d}": index for index in range(attr_count)}
    return make_span("request", end_ns=None, attributes=attributes)


def _manual_tracer(out: io.StringIO) -> TTYTracer:
    tracer = TTYTracer(out=out)
    tracer._running = True
    return tracer


def _patch_terminal_size(
    monkeypatch, *, columns: int = 120, lines: int = 24
) -> _TerminalSize:
    import stario.telemetry.tty as tty_module

    size = _TerminalSize(columns, lines)
    monkeypatch.setattr(tty_module, "_terminal_size", size)
    return size
