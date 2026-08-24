"""uvloop listener that uses the Cython protocol."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import format_datetime

import uvloop

from stario.http.app import App
from stario.http.bootstrap import Bootstrap, bootstrap_run
from stario.http.compression import compression_config_from_env
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.spans import ProxySpan

from stario_cython.protocol import HttpProtocol


def _date_header() -> bytes:
    return b"date: %s\r\n" % format_datetime(datetime.now(UTC), usegmt=True).encode(
        "ascii"
    )


async def serve(
    bootstrap: Bootstrap,
    host: str = "127.0.0.1",
    port: int = 8000,
    backlog: int = 2048,
) -> None:
    app = App()
    tracer = NoOpTracer()
    span = ProxySpan(tracer.create("server.startup"))
    span.start()
    compression = compression_config_from_env()
    connections: set[HttpProtocol] = set()
    date_box = [_date_header()]

    def factory() -> HttpProtocol:
        return HttpProtocol(
            asyncio.get_running_loop(),
            app,
            tracer,
            date_box,
            compression,
            connections,
        )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.signal_shutdown)

    async def tick_date() -> None:
        while True:
            await asyncio.sleep(1)
            date_box[0] = _date_header()

    async with bootstrap_run(bootstrap, app, span):
        clock = asyncio.create_task(tick_date())
        server = await loop.create_server(
            factory,
            host,
            port,
            backlog=backlog,
            reuse_address=True,
        )
        span.attr("server.listening", True)
        span.attr("server.http_core", "cython")
        span.attr("server.port", port)
        span.end()
        try:
            await app.shutdown
        finally:
            clock.cancel()
            with suppress(asyncio.CancelledError):
                await clock
            server.close()
            await server.wait_closed()
            for proto in list(connections):
                transport = proto.transport
                if transport is not None and not transport.is_closing():
                    transport.close()
            await app.drain_tasks()


def run(bootstrap: Bootstrap) -> None:
    host = os.environ.get("STARIO_HOST", "127.0.0.1")
    port = int(os.environ.get("STARIO_PORT", "8000"))
    uvloop.run(serve(bootstrap, host, port))
