"""Cython-backed HTTP server entry.

Lifecycle (signal handling, graceful drain, date-header refresh) is the
standard ``stario.http.server.Server``. This module injects the compiled
protocol factory and keeps the Cython defaults (uvloop + NoOpTracer).
Request-policy knobs the compiled parser does not implement (header/idle
timeouts, the 8-request pipeline cap) stay unwired.
"""

from __future__ import annotations

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
