"""Cython-backed HTTP server entry.

Lifecycle (signals, drain, Date tick) is ``stario.http.server.Server``.
This module only injects uvloop + ``NoOpTracer`` for
``python -m stario_cython``. Production CLI is ``stario serve``.
"""

from __future__ import annotations

import argparse
import sys

from stario.cli.imports import load_symbol
from stario.http.bootstrap import Bootstrap
from stario.http.config import ServerConfig, server_config_from_env
from stario.http.server import Server
from stario.telemetry.noop import NoOpTracer


def _uvloop_config(config: ServerConfig | None = None, **overrides) -> ServerConfig:
    cfg = config if config is not None else server_config_from_env()
    return ServerConfig(
        host=overrides.get("host", cfg.host),
        port=overrides.get("port", cfg.port),
        unix_socket=overrides.get("unix_socket", cfg.unix_socket),
        unix_socket_mode=cfg.unix_socket_mode,
        requests=cfg.requests,
        compression=cfg.compression,
        graceful_shutdown_timeout=cfg.graceful_shutdown_timeout,
        backlog=overrides.get("backlog", cfg.backlog),
        reuse_addr=cfg.reuse_addr,
        event_loop="uvloop",
        threads=cfg.threads,
        ssl=cfg.ssl,
    )


async def serve(
    bootstrap: Bootstrap,
    host: str = "127.0.0.1",
    port: int = 8000,
    backlog: int = 2048,
) -> None:
    """Run ``bootstrap`` behind the Cython HTTP protocol."""
    await Server(
        bootstrap,
        NoOpTracer(),
        config=_uvloop_config(host=host, port=port, backlog=backlog, unix_socket=None),
    ).serve()


def run(bootstrap: Bootstrap) -> None:
    """CLI / ``python -m stario_cython`` entry."""
    Server(bootstrap, NoOpTracer(), config=_uvloop_config()).run()


def main(argv: list[str] | None = None) -> int:
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
