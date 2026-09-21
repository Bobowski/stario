"""Shared test helpers for the stario test suite.

Plain importable helpers (not fixtures) so test modules can compose them
freely: `from tests.helpers import DummyWriter, make_context, ...`.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Generator
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import urlencode

from stario.http.app import App
from stario.http.compression import CompressionConfig
from stario.http.context import Context
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request
from stario.http.writer import Writer
from stario.telemetry.noop import NoOpTracer
from stario.testing.transport import MemoryTransport as _MemoryTransport

type AppSetup = Callable[[App], None]


def app_for_loop(_loop: asyncio.AbstractEventLoop) -> App:
    return App()


class DummyWriter:
    """Minimal Writer stand-in for dispatch-level router/app tests.

    Tracks the response surface the router/app touches: status, body,
    headers, started/ended/completed flags.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.body: str | None = None
        self.headers = Headers()
        self.started = False
        self.ended = False
        self._status_code: int | None = None
        self._completed = False

    @property
    def status_code(self) -> int | None:
        return self._status_code

    @property
    def completed(self) -> bool:
        return self._completed

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        self.body = body.decode("utf-8")
        self.status = status
        self.started = True
        self._status_code = status
        self.headers.set("content-type", content_type.decode("latin-1"))

    def write_headers(self, status: int):
        self.status = status
        self.started = True
        self._status_code = status
        return self

    def end(self, data: bytes | None = None) -> None:
        if data is not None:
            self.body = data.decode("utf-8")
        self.ended = True
        self._completed = True
        return None

    def abort(self) -> None:
        self.ended = False
        self._completed = True


def make_body_reader(body: bytes = b"", **kwargs: Any) -> BodyReader:
    """A completed BodyReader pre-fed through the public protocol hooks."""
    reader = BodyReader(pause=lambda: None, resume=lambda: None, **kwargs)
    if body:
        reader.feed(body)
    reader.complete()
    return reader


def make_request(
    *,
    method: str = "GET",
    path: str = "/",
    host: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: dict[str, object] | None = None,
    query_bytes: bytes | None = None,
) -> Request:
    hdrs = Headers()
    if headers:
        for name, value in headers.items():
            hdrs.set(name, value)
    if host:
        hdrs.set("host", host)

    if query_bytes is None:
        query_bytes = urlencode(query or {}, doseq=True).encode("ascii")

    return Request(
        method=method,
        path=path,
        query_bytes=query_bytes,
        headers=hdrs,
        body=make_body_reader(body),
    )


def make_context(
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    loop: asyncio.AbstractEventLoop,
    disconnect: asyncio.Future[Any] | None = None,
) -> Context:
    if disconnect is None:
        disconnect = loop.create_future()
    if app is None:
        app = App()
    tracer = NoOpTracer()
    return Context(
        app=app,
        req=make_request(method=method, path=path, host=host, query=query),
        span=tracer.create("request"),
        _disconnect=disconnect,
        state={},
    )


async def invoke_app(
    app: App,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[Context, DummyWriter]:
    loop = asyncio.get_running_loop()
    ctx = make_context(path, method, host, app=app, query=query, loop=loop)
    w = writer or DummyWriter()
    await app(ctx, cast(Writer, w))
    return ctx, w


def run_with_app(
    setup: AppSetup | App,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    query: dict[str, object] | None = None,
) -> tuple[Context, DummyWriter]:
    async def _run() -> tuple[Context, DummyWriter]:
        loop = asyncio.get_running_loop()
        if isinstance(setup, App):
            app = setup
        else:
            app = app_for_loop(loop)
            setup(app)
        return await invoke_app(app, path, method, host, query=query)

    return asyncio.run(_run())


async def invoke_handler(
    handler: Any,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[Context, DummyWriter]:
    loop = asyncio.get_running_loop()
    ctx = make_context(path, method, host, app=app, query=query, loop=loop)
    w = writer or DummyWriter()
    await handler(ctx, cast(Writer, w))
    return ctx, w


def run_handler(
    handler: Any,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[Context, DummyWriter]:
    return asyncio.run(
        invoke_handler(
            handler,
            path,
            method,
            host,
            app=app,
            query=query,
            writer=writer,
        )
    )


def run_async(awaitable: Awaitable[None]) -> None:
    asyncio.run(cast(Coroutine[Any, Any, None], awaitable))


@contextmanager
def make_writer(
    *,
    compression: CompressionConfig | None = None,
    accept_encoding: str | bytes = "",
) -> Generator[tuple[Writer, bytearray]]:
    """A real Writer wired to an in-memory sink, with loop cleanup."""
    loop = asyncio.new_event_loop()
    sink = bytearray()
    extra: dict[str, Any] = {}
    if compression is not None:
        extra["compression"] = compression
    try:
        transport = _MemoryTransport(sink.extend)
        writer = Writer(
            transport=transport,
            get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
            on_completed=lambda: None,
            accept_encoding=accept_encoding,
            **extra,
        )
        yield writer, sink
    finally:
        loop.close()


def make_writer_raw() -> tuple[Writer, bytearray, asyncio.AbstractEventLoop]:
    """Writer + sink + owning loop; caller must close the loop (try/finally)."""
    loop = asyncio.new_event_loop()
    sink = bytearray()
    transport = _MemoryTransport(sink.extend)
    writer = Writer(
        transport=transport,
        get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
        on_completed=lambda: None,
    )
    return writer, sink, loop


def split_response(raw: bytes) -> tuple[bytes, bytes]:
    """Split raw HTTP/1.1 wire bytes into (head, body)."""
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body
