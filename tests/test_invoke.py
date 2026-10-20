"""Handler-task finish: log and write 500 if nothing was sent."""

import asyncio
import contextlib
import logging

import pytest

from stario.http.app import App
from stario.http.context import Context
from stario.http.invoke import finish_request_span, on_handler_done
from stario.http.route import Route
from stario.http.writer import Writer
from stario.telemetry.noop import NoOpTracer
from stario.testing.tracer import TestTracer
from tests.helpers import DummyWriter, make_context


def test_finish_request_span_records_status_without_fail() -> None:
    with TestTracer() as tracer:
        span = tracer.create("request")
        finish_request_span(span, status=308, method="GET", path="/search/")
        finished = tracer.get_span(span.id)
        assert finished is not None
        assert finished.attributes["response.status_code"] == 308
        assert finished.attributes["request.method"] == "GET"
        assert finished.attributes["request.path"] == "/search/"
        assert finished.ok
        assert not tracer.has_open_spans()


def test_finish_request_span_skips_noop() -> None:
    span = NoOpTracer().create("request")
    finish_request_span(span, status=400)
    finish_request_span(None, status=413)


def test_find_handler_is_the_resolve_step() -> None:
    async def run() -> None:
        app = App()

        async def hello(_c: Context, w: Writer) -> None:
            w.text("ok")

        app.add(Route("GET /hello"), hello)
        ctx = make_context("/hello", app=app, loop=asyncio.get_running_loop())
        handler, _route, ctx.match = app.find_handler("", "/hello", "GET")
        assert handler is hello
        assert ctx.match.pattern == "GET /hello"

    asyncio.run(run())


def test_on_handler_done_writes_500_on_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
        assert w.status == 500
        assert w.body == "Internal Server Error"
        assert "Handler failed" in caplog.text

    asyncio.run(run())


def test_on_handler_done_writes_500_when_handler_writes_nothing(
    caplog: pytest.LogCaptureFixture,
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
        assert w.status == 500
        assert w.body == "Internal Server Error"
        assert "without sending a response" in caplog.text

    asyncio.run(run())


def test_on_handler_done_ends_started_response_on_clean_return(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        ctx = make_context("/x", app=app, loop=asyncio.get_running_loop())
        w = DummyWriter()
        w.write_headers(200)

        async def streamed() -> None:
            return None

        task = app.create_task(streamed(), eager_start=True)
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            on_handler_done(ctx, w, task)  # type: ignore[arg-type]
        assert w.completed
        assert w.ended
        assert "without sending a response" not in caplog.text
        assert "Handler failed" not in caplog.text

    asyncio.run(run())


def test_on_handler_done_aborts_if_headers_already_started(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        ctx = make_context("/x", app=app, loop=asyncio.get_running_loop())
        w = DummyWriter()
        w.write_headers(200)

        async def boom_after_headers() -> None:
            raise RuntimeError("after headers")

        task = app.create_task(boom_after_headers(), eager_start=True)
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            on_handler_done(ctx, w, task)  # type: ignore[arg-type]
        assert w.status == 200
        assert w.completed
        assert not w.ended
        assert "Handler failed" in caplog.text

    asyncio.run(run())


def test_on_handler_done_logs_exception_after_completed_response(
    caplog: pytest.LogCaptureFixture,
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


def test_on_handler_done_records_a_missing_response_on_the_span(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        with TestTracer() as tracer:
            ctx = make_context(
                "/ls/s9dk-f26u-6uuw/flip-item",
                method="POST",
                app=app,
                loop=asyncio.get_running_loop(),
            )
            span = tracer.create("request")
            span.start()
            ctx.span = span
            w = DummyWriter()

            async def silent() -> None:
                return None

            task = app.create_task(silent(), eager_start=True)
            with caplog.at_level(logging.ERROR, logger="stario.http"):
                on_handler_done(ctx, w, task)  # type: ignore[arg-type]
            assert w.status == 500
            assert "without sending a response" not in caplog.text
            finished = tracer.get_span(span.id)
            assert finished is not None
            assert finished.error == (
                "Handler returned without sending a response "
                "(POST /ls/s9dk-f26u-6uuw/flip-item)"
            )
            assert "request.client_left" not in finished.attributes
            assert not tracer.has_open_spans()

    asyncio.run(run())


def test_on_handler_done_does_not_fail_when_the_client_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        with TestTracer() as tracer:
            ctx = make_context("/ls/stream", app=app, loop=asyncio.get_running_loop())
            span = tracer.create("request")
            span.start()
            ctx.span = span
            w = DummyWriter()
            w.gone = True

            async def silent() -> None:
                return None

            task = app.create_task(silent(), eager_start=True)
            with caplog.at_level(logging.ERROR, logger="stario.http"):
                on_handler_done(ctx, w, task)  # type: ignore[arg-type]
            assert w.completed
            assert w.status is None
            assert "without sending a response" not in caplog.text
            assert "Handler failed" not in caplog.text
            finished = tracer.get_span(span.id)
            assert finished is not None
            assert finished.ok
            assert finished.attributes["request.client_left"] is True

    asyncio.run(run())


def test_on_handler_done_marks_client_left_when_the_task_was_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        with TestTracer() as tracer:
            ctx = make_context("/ls/stream", app=app, loop=asyncio.get_running_loop())
            span = tracer.create("request")
            span.start()
            ctx.span = span
            w = DummyWriter()
            w.gone = True

            async def hang() -> None:
                await asyncio.sleep(60)

            task = app.create_task(hang(), eager_start=True)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert task.cancelled()

            with caplog.at_level(logging.ERROR, logger="stario.http"):
                on_handler_done(ctx, w, task)  # type: ignore[arg-type]
            assert w.completed
            assert "Handler failed" not in caplog.text
            finished = tracer.get_span(span.id)
            assert finished is not None
            assert finished.ok
            assert finished.attributes["request.client_left"] is True

    asyncio.run(run())


def test_on_handler_done_cancelled_while_connected_records_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        app = App()
        with TestTracer() as tracer:
            ctx = make_context("/ls/stream", app=app, loop=asyncio.get_running_loop())
            span = tracer.create("request")
            span.start()
            ctx.span = span
            w = DummyWriter()  # client still connected

            async def hang() -> None:
                await asyncio.sleep(60)

            task = app.create_task(hang(), eager_start=True)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert task.cancelled()

            with caplog.at_level(logging.ERROR, logger="stario.http"):
                on_handler_done(ctx, w, task)  # type: ignore[arg-type]
            assert w.completed
            finished = tracer.get_span(span.id)
            assert finished is not None
            assert "request.client_left" not in finished.attributes

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
