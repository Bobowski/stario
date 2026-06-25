"""TTY tracer tests.

Formatting is verified through `TTYRenderer` on hand-built `RecordingSpan` records.
Tracer lifecycle is verified through the public context-manager API with an injected `out`
stream.
"""

import io
import os
from uuid import UUID, uuid4, uuid7

from stario.telemetry.noop import NoOpTracer
from stario.telemetry.spans import RecordedEvent, RecordedLink, RecordingSpan
from stario.telemetry.tty import TTYRenderer, TTYTracer

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


class TestTracerPublicLifecycle:
    """Exercises `TTYTracer` only through its public surface (`out=`, `with`, spans)."""

    def test_context_manager_flushes_open_spans_on_exit(self, monkeypatch):
        _patch_terminal_width(monkeypatch)
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


def _patch_terminal_width(monkeypatch) -> None:
    import stario.telemetry.tty as tty_module

    def fake_terminal_size(_fallback: os.terminal_size) -> os.terminal_size:
        return os.terminal_size((120, 24))

    monkeypatch.setattr(
        tty_module.shutil,
        "get_terminal_size",
        fake_terminal_size,
    )
