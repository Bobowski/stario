"""Isolate GET /plaintext costs on a running uvloop.

    PYTHONPATH=src:. .venv/bin/python benchmarks/server/profile_plaintext.py
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import uvloop

import stario.responses as responses
from stario_cython.app import App
from stario_cython.router import Router
from tests.cython.http import make_protocol
from tests.helpers import DummyWriter, make_context

HELLO = "Hello, World!"
HELLO_B = HELLO.encode("utf-8")
CT = b"text/plain; charset=utf-8"


def _bench(name: str, fn: Callable[[], None], n: int) -> None:
    fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = time.perf_counter() - t0
    print(f"{name:52s}  {n / dt:10,.0f}/s   {1e9 * dt / n:7.0f} ns")


async def _plain(_c, w) -> None:
    responses.text(w, HELLO)


async def _plain_bytes(_c, w) -> None:
    w.respond(HELLO_B, CT, 200)


async def _empty() -> None:
    return None


async def _nested() -> None:
    await _empty()


async def main() -> None:
    loop = asyncio.get_running_loop()
    print(f"loop {type(loop).__module__}.{type(loop).__name__}")

    router = Router()
    router.get("/plaintext", _plain)
    router.get("/user/{user_id}", _plain)

    _bench("router static /plaintext", lambda: router.find_handler("", "/plaintext", "GET"), 300_000)
    _bench("router param /user/42", lambda: router.find_handler("", "/user/42", "GET"), 300_000)
    _bench("str.encode Hello, World!", HELLO.encode, 300_000)

    import ujson

    def dumps() -> None:
        ujson.dumps({"message": HELLO}).encode("utf-8")

    _bench("ujson.dumps json body", dumps, 200_000)

    n = 80_000
    done = 0
    t0 = time.perf_counter()
    for _ in range(n):
        t = asyncio.Task(_empty(), loop=loop, eager_start=True)
        if t.done():
            done += 1
    dt = time.perf_counter() - t0
    print(
        f"{'asyncio.Task empty eager':52s}  {n / dt:10,.0f}/s   {1e9 * dt / n:7.0f} ns"
        f"  (done={done}/{n})"
    )

    done = 0
    t0 = time.perf_counter()
    for _ in range(n):
        t = asyncio.Task(_nested(), loop=loop, eager_start=True)
        if t.done():
            done += 1
    dt = time.perf_counter() - t0
    print(
        f"{'asyncio.Task nested await eager':52s}  {n / dt:10,.0f}/s   {1e9 * dt / n:7.0f} ns"
        f"  (done={done}/{n})"
    )

    def alloc_coro() -> None:
        _empty().close()

    _bench("alloc empty coroutine", alloc_coro, 300_000)

    app = App()
    app.get("/plaintext", _plain)
    app.get("/plain-bytes", _plain_bytes)
    ctx = make_context("/plaintext", app=app, loop=loop)
    ctx_b = make_context("/plain-bytes", app=app, loop=loop)
    dummy = DummyWriter()

    async def call(ctx_obj) -> None:
        dummy.started = False
        dummy.ended = False
        dummy._completed = False
        dummy.status = None
        dummy.body = None
        dummy._status_code = None
        await app(ctx_obj, dummy)

    # Drive through the same Task path the protocol uses.
    def call_text() -> None:
        dummy.started = False
        dummy.ended = False
        dummy._completed = False
        dummy.status = None
        dummy.body = None
        dummy._status_code = None
        t = app.create_task(app(ctx, dummy), loop=loop, eager_start=True)
        if not t.done():
            raise RuntimeError("expected eager completion")

    def call_bytes() -> None:
        dummy.started = False
        dummy.ended = False
        dummy._completed = False
        dummy.status = None
        dummy.body = None
        dummy._status_code = None
        t = app.create_task(app(ctx_b, dummy), loop=loop, eager_start=True)
        if not t.done():
            raise RuntimeError("expected eager completion")

    _bench("App+Task DummyWriter responses.text", call_text, 60_000)
    _bench("App+Task DummyWriter preencoded respond", call_bytes, 60_000)

    def drive_send() -> None:
        dummy.started = False
        dummy.ended = False
        dummy._completed = False
        dummy.status = None
        dummy.body = None
        dummy._status_code = None
        coro = app(ctx, dummy)
        try:
            coro.send(None)
        except StopIteration:
            pass

    _bench("App coro.send DummyWriter (no Task)", drive_send, 60_000)

    def drive_handler_only() -> None:
        dummy.started = False
        dummy.ended = False
        dummy._completed = False
        dummy.status = None
        dummy.body = None
        dummy._status_code = None
        handler, route = app.find_handler("", "/plaintext", "GET")
        ctx.route = route
        coro = handler(ctx, dummy)
        try:
            coro.send(None)
        except StopIteration:
            pass

    _bench("handler coro.send + find_handler (no App)", drive_handler_only, 60_000)

    def drive_text_only() -> None:
        dummy.started = False
        dummy.ended = False
        dummy._completed = False
        dummy.status = None
        dummy.body = None
        dummy._status_code = None
        responses.text(dummy, HELLO)

    _bench("responses.text DummyWriter only", drive_text_only, 200_000)

    # Live protocol: parse one GET and dispatch (includes llhttp + respond + uvloop transport).
    connections: set = set()
    date = [b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"]
    proto = make_protocol(loop, app, connections=connections, date=date[0])

    class FakeTransport:
        def __init__(self) -> None:
            self.writes = 0
            self._closing = False

        def write(self, data) -> None:
            self.writes += 1

        def writelines(self, lines) -> None:
            self.writes += 1

        def is_closing(self) -> bool:
            return self._closing

        def close(self) -> None:
            self._closing = True

        def get_extra_info(self, name, default=None):
            return default

        def pause_reading(self) -> None:
            return None

        def resume_reading(self) -> None:
            return None

    req = (
        b"GET /plaintext HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"\r\n"
    )

    def one_http() -> None:
        transport = FakeTransport()
        proto.connection_made(transport)
        proto.data_received(req)
        proto.connection_lost(None)

    # connection_made each time is heavier than keepalive. Time keepalive reuse:
    transport = FakeTransport()
    proto.connection_made(transport)
    proto.data_received(req)  # warmup

    def keepalive_get() -> None:
        proto.data_received(req)

    _bench("protocol keepalive GET /plaintext", keepalive_get, 40_000)
    print(f"fake transport writes after bench: {transport.writes}")

    import cProfile
    import pstats
    from io import StringIO

    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(20_000):
        keepalive_get()
    profiler.disable()
    stream = StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("tottime")
    stats.print_stats(35)
    print("\n--- cProfile keepalive GET /plaintext (20k) ---")
    print(stream.getvalue())


if __name__ == "__main__":
    uvloop.run(main())
