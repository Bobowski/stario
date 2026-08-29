"""Handler-task finish: log and abort, do not map exceptions to HTTP."""

import asyncio
import logging

import stario.responses as responses
from stario.http.app import App
from stario.http.context import Context
from stario.http.invoke import on_handler_done
from stario.http.writer import Writer
from tests.helpers import DummyWriter, make_context


def test_find_handler_is_the_resolve_step() -> None:
    async def run() -> None:
        app = App()

        async def hello(_c: Context, w: Writer) -> None:
            responses.text(w, "ok")

        app.get("/hello", hello)
        ctx = make_context("/hello", app=app, loop=asyncio.get_running_loop())
        handler, ctx.route = app.find_handler("", "/hello", "GET")
        assert handler is hello
        assert ctx.route.pattern == "/hello"

    asyncio.run(run())


def test_on_handler_done_aborts_on_exception(caplog: logging.LogCaptureFixture) -> None:
    async def run() -> None:
        app = App()
        ctx = make_context("/x", app=app, loop=asyncio.get_running_loop())
        w = DummyWriter()

        async def boom() -> None:
            raise RuntimeError("boom")

        task = app.create_task(boom(), eager_start=True)
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            on_handler_done(ctx, w, task)  # type: ignore[arg-type]
        assert w.completed
        assert w.status is None
        assert "Handler failed" in caplog.text

    asyncio.run(run())


def test_on_handler_done_aborts_when_handler_writes_nothing(
    caplog: logging.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        ctx = make_context("/x", app=app, loop=asyncio.get_running_loop())
        w = DummyWriter()

        async def silent() -> None:
            return None

        task = app.create_task(silent(), eager_start=True)
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            on_handler_done(ctx, w, task)  # type: ignore[arg-type]
        assert w.completed
        assert w.status is None
        assert "without sending a response" in caplog.text

    asyncio.run(run())


def test_on_handler_done_logs_exception_after_completed_response(
    caplog: logging.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        ctx = make_context("/x", app=app, loop=asyncio.get_running_loop())
        w = DummyWriter()
        w.respond(b"ok", b"text/plain", 200)

        async def boom_after_write() -> None:
            raise RuntimeError("after write")

        task = app.create_task(boom_after_write(), eager_start=True)
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            on_handler_done(ctx, w, task)  # type: ignore[arg-type]
        assert w.status == 200
        assert w.body == "ok"
        assert w.completed
        assert "Handler failed" in caplog.text

    asyncio.run(run())


def test_on_handler_done_does_not_end_a_completed_response() -> None:
    async def run() -> None:
        app = App()
        ctx = make_context("/x", app=app, loop=asyncio.get_running_loop())
        w = DummyWriter()
        w.respond(b"ok", b"text/plain", 200)

        async def already_done() -> None:
            return None

        task = app.create_task(already_done(), eager_start=True)
        on_handler_done(ctx, w, task)  # type: ignore[arg-type]
        assert w.status == 200
        assert w.body == "ok"

    asyncio.run(run())
