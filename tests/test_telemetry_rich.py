import io

import pytest
from rich.console import Console
from rich.text import Text

import stario.telemetry.rich as rich_module
from stario.telemetry.rich import RichTracer, _fmt_duration


class FakeLive:
    instances: list["FakeLive"] = []

    def __init__(
        self,
        renderable,
        *,
        console,
        transient: bool,
        auto_refresh: bool,
    ) -> None:
        self.renderable = renderable
        self.console = console
        self.transient = transient
        self.auto_refresh = auto_refresh
        self.started = False
        self.stopped = False
        self.updates: list[tuple[object, bool]] = []
        self.instances.append(self)

    def start(self) -> None:
        self.started = True

    def update(self, renderable, *, refresh: bool) -> None:
        self.renderable = renderable
        self.updates.append((renderable, refresh))

    def stop(self) -> None:
        self.stopped = True


def test_rich_tracer_records_changes_until_render_tick(monkeypatch) -> None:
    output = io.StringIO()
    FakeLive.instances.clear()
    monkeypatch.setattr(rich_module, "Live", FakeLive)

    tracer = RichTracer()
    tracer._running = True
    tracer.console = Console(file=output, force_terminal=False, width=120)

    span = tracer.create("first.request")
    span.start()

    assert output.getvalue() == ""
    assert FakeLive.instances == []

    tracer._render()

    assert len(FakeLive.instances) == 1
    assert FakeLive.instances[0].started is True
    assert output.getvalue() == ""


def test_rich_tracer_prints_finished_roots_and_restarts_live(
    monkeypatch,
) -> None:
    output = io.StringIO()
    FakeLive.instances.clear()
    monkeypatch.setattr(rich_module, "Live", FakeLive)

    tracer = RichTracer()
    tracer._running = True
    tracer.console = Console(file=output, force_terminal=False, width=120)

    first = tracer.create("first.request")
    second = tracer.create("second.request")

    first.start()
    second.start()
    tracer._render()

    assert len(FakeLive.instances) == 1
    assert FakeLive.instances[0].started is True

    first.end()

    assert output.getvalue() == ""

    tracer._render()

    rendered = output.getvalue()
    assert "first.request" in rendered
    assert "second.request" not in rendered
    assert FakeLive.instances[0].stopped is False
    assert len(FakeLive.instances) == 1
    assert FakeLive.instances[0].updates
    assert FakeLive.instances[0].updates[-1][1] is True

    second.end()
    tracer._render()

    rendered = output.getvalue()
    assert "second.request" in rendered
    assert isinstance(FakeLive.instances[0].renderable, Text)
    assert FakeLive.instances[0].renderable.plain == " "


def test_fmt_duration_handles_sub_millisecond_spans() -> None:
    assert _fmt_duration(250_000) == "250 us"
    assert _fmt_duration(1_500_000) == "1.5 ms"


def test_rich_tracer_end_requires_explicit_start() -> None:
    tracer = RichTracer()
    span = tracer.create("request")

    with pytest.raises(RuntimeError, match="never started"):
        span.end()
