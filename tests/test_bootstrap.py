"""Tests for `stario.http.bootstrap` startup/shutdown driving."""

import pytest

from stario import App
from stario.exceptions import StarioError
from stario.http.bootstrap import bootstrap_run
from stario.telemetry.noop import NoOpTracer


def _test_span():
    return NoOpTracer().create("test.bootstrap")


@pytest.mark.asyncio
async def test_bootstrap_run_startup_and_shutdown() -> None:
    events: list[str] = []

    async def bootstrap(app: App, span):
        events.append("setup")
        yield
        events.append("teardown")

    app = App()
    async with bootstrap_run(bootstrap, app, _test_span()):
        events.append("body")

    assert events == ["setup", "body", "teardown"]


@pytest.mark.asyncio
async def test_bootstrap_run_closes_generator_on_startup_failure() -> None:
    class Resource:
        def __init__(self) -> None:
            self.entered = False
            self.exited = False

        async def __aenter__(self) -> Resource:
            self.entered = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.exited = True

    resource = Resource()

    async def bootstrap(app: App, span):
        async with resource:
            raise RuntimeError("startup failed")
        yield

    with pytest.raises(RuntimeError, match="startup failed"):
        async with bootstrap_run(bootstrap, App(), _test_span()):
            pass

    assert resource.entered
    assert resource.exited


@pytest.mark.asyncio
async def test_bootstrap_rejects_invalid_shapes() -> None:
    async def plain_async(app: App, span) -> None:
        pass

    async def multi_yield(app: App, span):
        yield
        yield

    def sync_fn(app: App, span) -> None:
        pass

    cases = [
        (plain_async, "must yield exactly once"),
        (sync_fn, "must be an async generator"),
        (multi_yield, "must not yield more than once"),
    ]

    for bootstrap, match in cases:
        with pytest.raises(StarioError, match=match):
            async with bootstrap_run(bootstrap, App(), _test_span()):  # type: ignore[arg-type]
                pass
