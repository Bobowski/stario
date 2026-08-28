"""Cython-backed HTTP server entry.

Lifecycle (signal handling, graceful drain, date-header refresh) is the
standard ``stario.http.server.Server``. This module injects the compiled
protocol factory and keeps the Cython defaults (uvloop + NoOpTracer).

Header, idle, and body-stall timeouts share one cleanup: a sweeper over the
connection set calls ``loop.time()`` once per wake and compares stored
deadlines. Header timeout is armed only while headers (or a deferred small
body) are still arriving. Idle/keep-alive timeout is armed only when the
connection has no in-flight request. Body stall is a generation counter on
the exchange — chunks do not ``call_later``. ``RequestPolicy.max_pipelined_requests``
(default 8) caps the pipeline queue.

``STARIO_CYTHON_TIMEOUTS=callback`` restores per-connection TimerHandles for
wrk A/B. ``STARIO_CYTHON_TIMEOUTS=off`` disables header/idle/body-stall
cleanup (profiling).
"""

from __future__ import annotations

import argparse
import sys

from stario.cli.imports import load_symbol
from stario.http.bootstrap import Bootstrap
from stario.http.config import RequestPolicy, ServerConfig, server_config_from_env
from stario.http.server import Server
from stario.telemetry.noop import NoOpTracer
from stario_cython.protocol import HttpProtocol


def _make_cython_protocol(
    loop,
    app,
    tracer,
    date_box,
    compression,
    connections,
    requests: RequestPolicy,
):
    return HttpProtocol(
        loop,
        app,
        tracer,
        date_box,
        compression,
        connections,
        max_header_bytes=requests.max_header_bytes,
        max_body_bytes=requests.max_body_bytes,
        header_timeout=requests.header_timeout,
        keep_alive_timeout=requests.keep_alive_timeout,
        body_timeout=requests.body_timeout,
        max_pipelined_requests=requests.max_pipelined_requests,
    )


def _uvloop_config(
    config: ServerConfig | None = None,
    **overrides,
) -> ServerConfig:
    cfg = config if config is not None else server_config_from_env()
    values = {
        "host": cfg.host,
        "port": cfg.port,
        "unix_socket": cfg.unix_socket,
        "unix_socket_mode": cfg.unix_socket_mode,
        "requests": cfg.requests,
        "compression": cfg.compression,
        "graceful_shutdown_timeout": cfg.graceful_shutdown_timeout,
        "backlog": cfg.backlog,
        "reuse_addr": cfg.reuse_addr,
        "event_loop": "uvloop",
    }
    values.update(overrides)
    return ServerConfig(**values)


def _server(bootstrap: Bootstrap, config: ServerConfig) -> Server:
    return Server(
        bootstrap,
        NoOpTracer(),
        config=config,
        make_protocol=_make_cython_protocol,
    )


async def serve(
    bootstrap: Bootstrap,
    host: str = "127.0.0.1",
    port: int = 8000,
    backlog: int = 2048,
) -> None:
    """Run ``bootstrap`` behind the Cython HTTP protocol."""
    await _server(
        bootstrap,
        _uvloop_config(
            host=host,
            port=port,
            backlog=backlog,
            unix_socket=None,
        ),
    ).serve()


def run(bootstrap: Bootstrap) -> None:
    """CLI / ``python -m stario_cython`` entry."""
    _server(bootstrap, _uvloop_config()).run()


def main(argv: list[str] | None = None) -> int:
    """Parse ``MODULE:bootstrap`` and run the Cython server."""
    parser = argparse.ArgumentParser(prog="stario-cython")
    parser.add_argument(
        "app",
        metavar="MODULE:CALLABLE",
        help="Import path to bootstrap (async def bootstrap(app, span): ...; yield)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    bootstrap = load_symbol(args.app, label="app")
    if not callable(bootstrap):
        parser.error("app must be callable")
    run(bootstrap)  # type: ignore[arg-type]
    return 0
