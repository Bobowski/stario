"""Unit tests for exchange-level resolve / finish (no App envelope)."""

import asyncio

import stario.responses as responses
from stario.exceptions import ClientDisconnected, HttpException, RedirectException
from stario.http.app import App
from stario.http.context import Context
from stario.http.invoke import apply_failure, resolve_handler, schedule_request
from stario.http.writer import Writer
from tests.helpers import DummyWriter, make_context


def test_resolve_handler_uses_find_handler() -> None:
    async def run() -> None:
        app = App()

        async def hello(_c: Context, w: Writer) -> None:
            responses.text(w, "ok")

        app.get("/hello", hello)
        ctx = make_context("/hello", app=app, loop=asyncio.get_running_loop())
        handler = resolve_handler(app, ctx)
        assert handler is hello
        assert ctx.route.pattern == "/hello"

    asyncio.run(run())


def test_resolve_trailing_slash_skips_trie() -> None:
    async def run() -> None:
        app = App()
        seen: list[str] = []

        async def hello(_c: Context, w: Writer) -> None:
            seen.append("hello")
            responses.text(w, "ok")

        app.get("/hello", hello)
        ctx = make_context("/hello/", app=app, loop=asyncio.get_running_loop())
        handler = resolve_handler(app, ctx)
        assert handler is not hello
        await handler(ctx, DummyWriter())  # type: ignore[arg-type]
        assert seen == []

    asyncio.run(run())


def test_apply_failure_maps_only_builtin_types() -> None:
    w = DummyWriter()
    assert apply_failure(None, w, HttpException(404, "nope")) is False  # type: ignore[arg-type]
    assert w.status == 404
    assert w.body == "nope"

    w = DummyWriter()
    assert apply_failure(None, w, RedirectException(303, "/next")) is False  # type: ignore[arg-type]
    assert w.status == 303

    w = DummyWriter()
    assert apply_failure(None, w, ClientDisconnected()) is False  # type: ignore[arg-type]
    assert w.completed
    assert w.status is None

    w = DummyWriter()
    assert apply_failure(None, w, ValueError("nope")) is True  # type: ignore[arg-type]
    assert w.status == 500
    assert w.body == "Internal Server Error"


def test_schedule_request_runs_handler_not_app_call() -> None:
    async def run() -> None:
        app = App()
        calls: list[str] = []

        async def hello(_c: Context, w: Writer) -> None:
            calls.append("handler")
            responses.text(w, "ok")

        app.get("/x", hello)

        original = app.__call__

        async def wrapped(c: Context, w: Writer) -> None:
            calls.append("app")
            await original(c, w)

        app.__call__ = wrapped  # type: ignore[method-assign]

        ctx = make_context("/x", app=app, loop=asyncio.get_running_loop())
        w = DummyWriter()
        task = schedule_request(app, ctx, w, eager_start=True)  # type: ignore[arg-type]
        if not task.done():
            await task
        assert w.status == 200
        assert calls == ["handler"]

    asyncio.run(run())
