import io
import os
from uuid import uuid4

import pytest

import stario.telemetry.tty as tty_module
from stario.telemetry.tty import TTYTracer, _fmt_duration


class FakeLive:
    instances: list["FakeLive"] = []

    def __init__(self, out: io.StringIO, lock) -> None:
        self._out = out
        self._lock = lock
        self.renderable: str | None = None
        self.stopped = False
        self.updates: list[str] = []
        self.instances.append(self)

    def erase(self) -> None:
        pass

    def write(self, content: str) -> None:
        self.renderable = content
        self.updates.append(content)

    def stop(self) -> None:
        self.stopped = True


def _patch_tty(monkeypatch) -> None:
    monkeypatch.setattr(
        tty_module.shutil,
        "get_terminal_size",
        lambda _: os.terminal_size((120, 24)),
    )


def test_tty_tracer_records_changes_until_render_tick(monkeypatch) -> None:
    output = io.StringIO()
    FakeLive.instances.clear()
    monkeypatch.setattr(tty_module, "_LiveRegion", FakeLive)
    _patch_tty(monkeypatch)

    tracer = TTYTracer()
    tracer._running = True
    tracer._out = output

    span = tracer.create("first.request")
    span.start()

    assert output.getvalue() == ""
    assert FakeLive.instances == []

    tracer._render()

    assert len(FakeLive.instances) == 1
    assert FakeLive.instances[0].updates
    assert output.getvalue() == ""


def test_tty_tracer_prints_finished_roots_and_restarts_live(
    monkeypatch,
) -> None:
    output = io.StringIO()
    FakeLive.instances.clear()
    monkeypatch.setattr(tty_module, "_LiveRegion", FakeLive)
    _patch_tty(monkeypatch)

    tracer = TTYTracer()
    tracer._running = True
    tracer._out = output

    first = tracer.create("first.request")
    second = tracer.create("second.request")

    first.start()
    second.start()
    tracer._render()

    assert len(FakeLive.instances) == 1
    assert FakeLive.instances[0].updates

    first.end()

    assert output.getvalue() == ""

    tracer._render()

    rendered = output.getvalue()
    assert "first.request" in rendered
    assert "second.request" not in rendered
    assert FakeLive.instances[0].stopped is False
    assert len(FakeLive.instances) == 1
    assert FakeLive.instances[0].updates

    second.end()
    tracer._render()

    rendered = output.getvalue()
    assert "second.request" in rendered
    assert isinstance(FakeLive.instances[0].renderable, str)
    assert FakeLive.instances[0].renderable == " "


def test_fmt_duration_handles_sub_millisecond_spans() -> None:
    assert _fmt_duration(250_000) == "250 us"
    assert _fmt_duration(1_500_000) == "1.5 ms"


def test_tty_tracer_end_requires_explicit_start() -> None:
    tracer = TTYTracer()
    span = tracer.create("request")

    with pytest.raises(RuntimeError, match="never started"):
        span.end()


def test_tty_tracer_printed_panel_includes_links(monkeypatch) -> None:
    output = io.StringIO()
    FakeLive.instances.clear()
    monkeypatch.setattr(tty_module, "_LiveRegion", FakeLive)
    _patch_tty(monkeypatch)

    tracer = TTYTracer()
    tracer._running = True
    tracer._out = output

    target = uuid4()
    root = tracer.create("with-link")
    root.start()
    tracer.add_link(root.id, target, {"kind": "test"})
    root.end()
    tracer._render()

    text = output.getvalue()
    assert str(target)[-8:] in text
    assert "kind=test" in text or "kind" in text
