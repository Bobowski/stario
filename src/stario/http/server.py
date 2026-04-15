"""
Runs ``App`` behind an asyncio listener: signal handling, bootstrap context, graceful drain, then socket teardown.

Bootstrap completes before ``start_serving``; exceptions there fail startup loudly. Transport policy (TCP vs Unix, backlog,
compression defaults) lives here so ``Router``/``App`` stay free of process-level concerns.
"""

import asyncio
import os
import signal
import socket
import stat
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timezone
from email.utils import format_datetime
from types import FrameType
from typing import Any, AsyncContextManager, Literal

from stario.exceptions import StarioError
from stario.telemetry import Tracer
from stario.telemetry.core import Span

from .app import App
from .protocol import HttpProtocol
from .request import DEFAULT_MAX_BODY_SIZE, DEFAULT_MAX_HEADER_BYTES
from .writer import CompressionConfig

type AppBootstrap = Callable[[App, Span], AsyncContextManager[None]]
type AppFactory = Callable[[], App]


type RunState = Literal[
    "ready",
    "starting",
    "running",
    "shutting_down",
    "stopped",
]
type ShutdownTrigger = Literal[
    "expected_stop",
    "runtime_failure",
    "fallback_cleanup",
]
type SignalHandler = Callable[[int, FrameType | None], object]
type PreviousSignalHandler = signal.Handlers | int | SignalHandler | None


class Server:
    """Binds a listener, runs the bootstrap context around a fresh app, serves until SIGINT/SIGTERM, then drains."""

    def __init__(
        self,
        bootstrap: AppBootstrap,
        tracer: Tracer,
        *,
        app_factory: AppFactory | None = None,
        host: str = "127.0.0.1",
        port: int = 8000,
        graceful_timeout: float = 5.0,
        backlog: int = 2048,
        unix_socket: str | None = None,
        unix_socket_mode: int = 0o660,
        compression: CompressionConfig = CompressionConfig(),
        event_loop_name: str | None = None,
        max_request_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_request_body_bytes: int = DEFAULT_MAX_BODY_SIZE,
    ) -> None:
        """Configure listening, bootstrap, telemetry, and per-connection compression.

        Parameters:
            bootstrap: Async context manager factory ``(app, span)`` run around the serving loop.
            tracer: Telemetry backend implementing the ``Tracer`` protocol.
            app_factory: Builds each run's ``App``; defaults to ``App``.
            host: TCP bind address (ignored when ``unix_socket`` is set).
            port: TCP port.
            graceful_timeout: Seconds to wait for open connections and ``App.create_task`` work during shutdown.
            backlog: Socket listen backlog.
            unix_socket: Filesystem path for a Unix domain listener; removes stale socket files before bind.
            unix_socket_mode: Mode applied after bind on the Unix socket path (e.g. group read for a proxy user).
            compression: Copied into each new ``Writer`` for the lifetime of the connection.
            event_loop_name: Optional span attribute for identifying the chosen event loop implementation.
            max_request_header_bytes: Maximum combined size of the request line (method + URL) and all
                request headers before the body; larger requests receive ``431`` and the connection closes.
            max_request_body_bytes: Maximum bytes buffered for the request body (``413`` when exceeded).
        """
        if max_request_header_bytes < 256:
            raise StarioError(
                "max_request_header_bytes must be at least 256",
                help_text="Increase the limit or use the default Server settings.",
            )
        if max_request_body_bytes < 1:
            raise StarioError(
                "max_request_body_bytes must be at least 1",
                help_text="Use a positive byte limit for request bodies.",
            )
        if app_factory is None:
            app_factory = App
        self.bootstrap = bootstrap
        self.app_factory = app_factory
        self.host = host
        self.port = port
        self.graceful_timeout = graceful_timeout
        self.backlog = backlog
        self.unix_socket = unix_socket
        self.unix_socket_mode = unix_socket_mode
        self.compression = compression
        self.tracer = tracer
        self.event_loop_name = event_loop_name
        self.max_request_header_bytes = max_request_header_bytes
        self.max_request_body_bytes = max_request_body_bytes

        self._state: RunState = "ready"
        self._sock: socket.socket | None = None
        self._date_header = b""

    async def run(self) -> None:
        """Block until SIGINT/SIGTERM (or fatal error): create app, enter bootstrap, serve, drain, tear down.

        Raises:
            StarioError: If ``run`` is called twice on the same instance, or bootstrap suppresses startup errors.

        Notes:
            Requires an already-running event loop. Signals are temporarily replaced for SIGINT/SIGTERM during the call.
        """
        if self._state != "ready":
            raise StarioError(
                "Server already running",
                help_text="Create a new Server instance to run multiple servers.",
            )
        self._state = "starting"
        loop = asyncio.get_running_loop()
        shutdown_future: asyncio.Future[None] = loop.create_future()
        span = self.tracer.create("server.startup")
        # Explicitly start startup timing before bootstrap/server setup work.
        span.start()
        self._record_startup_attrs(span)

        def start_shutdown_span(trigger: ShutdownTrigger) -> None:
            if self._state == "starting":
                return
            if self._state == "shutting_down":
                return
            shutdown_span = self.tracer.create("server.shutdown")
            shutdown_span.link(span)
            shutdown_span.attr("server.shutdown.trigger", trigger)
            shutdown_span.start()
            # External code may hold the original startup span id; repoint it so end()/metrics close shutdown.
            span.id = shutdown_span.id
            self._state = "shutting_down"

        # Single ``_state``: guards re-entrancy, chooses which span ends on error,
        # and distinguishes "bootstrap failed" from "server was running".

        with self._signal_handlers(loop, shutdown_future):
            try:
                app = self.app_factory()
                async with (
                    self.bootstrap(app, span),
                    self._server_scope(
                        loop=loop,
                        span=span,
                        app=app,
                        shutdown_future=shutdown_future,
                    ),
                    self._date_tick_scope(),
                ):
                    # Startup succeeded; shutdown span begins only once shutdown starts.
                    span.end()
                    self._state = "running"

                    # Keep run() blocked until SIGINT/SIGTERM resolves this future.
                    await shutdown_future
                    start_shutdown_span("expected_stop")

                # Bootstrapping should not quietly finish before server startup.
                if self._state == "starting":
                    raise StarioError(
                        "Bootstrap suppressed startup failure",
                        help_text="Do not suppress exceptions before server startup completes.",
                    )

            except BaseException as exc:
                if self._state == "running":
                    start_shutdown_span("runtime_failure")
                span.exception(exc)
                span.fail(str(exc))
                raise

            finally:
                if self._state == "starting":
                    span.end()
                elif self._state == "running":
                    # Defensive fallback for unexpected exits without signal/error path.
                    start_shutdown_span("fallback_cleanup")
                if self._state == "shutting_down":
                    span.end()
                self._state = "stopped"

    @asynccontextmanager
    async def _server_scope(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        span: Span,
        app: "App",
        shutdown_future: asyncio.Future[None],
    ) -> AsyncIterator[None]:
        # Shared sets let protocol instances report active connections/tasks for shutdown.
        connections: set[HttpProtocol] = set()
        server: asyncio.Server | None = None
        server_started = False

        try:

            def protocol_factory() -> HttpProtocol:
                return HttpProtocol(
                    loop,
                    app,
                    self.tracer,
                    lambda: self._date_header,
                    self.compression,
                    shutdown_future,
                    connections,
                    max_request_header_bytes=self.max_request_header_bytes,
                    max_request_body_bytes=self.max_request_body_bytes,
                )

            if self.unix_socket:
                self._sock = self._create_unix_socket()
                server = await loop.create_unix_server(
                    protocol_factory,
                    sock=self._sock,
                    start_serving=False,
                )
            else:
                server = await loop.create_server(
                    protocol_factory,
                    self.host,
                    self.port,
                    backlog=self.backlog,
                    start_serving=False,
                )

            await server.start_serving()
            server_started = True
            span.attr("server.listening", True)
            yield

        finally:
            try:
                await self._shutdown_server(
                    server,
                    server_started,
                    connections,
                    app._tasks,
                    span,
                )
            finally:
                self._cleanup_unix_socket()

    async def _shutdown_server(
        self,
        server: asyncio.Server | None,
        server_started: bool,
        connections: set[HttpProtocol],
        tasks: set[asyncio.Task[Any]],
        span: Span,
    ) -> None:
        if server is None:
            return

        server.close()
        if not server_started:
            # If start_serving() failed, close low-level server resources
            # without emitting shutdown runtime metrics.
            await server.wait_closed()
            return

        open_connections = len(connections)
        await self._wait_for_managed_work_to_drain(connections, tasks)
        force_closed = await self._force_close_open_transports(connections)
        cancelled_tasks = await self._cancel_pending_tasks(tasks)

        span.attrs(
            {
                "server.shutdown.open_connections": open_connections,
                "server.shutdown.force_closed": force_closed,
                "server.shutdown.cancelled_tasks": cancelled_tasks,
            }
        )
        await server.wait_closed()
        span.event("server.shutdown.closed")

    async def _wait_for_managed_work_to_drain(
        self,
        connections: set[HttpProtocol],
        tasks: set[asyncio.Task[Any]],
    ) -> None:
        # Give open connections and server-managed tasks one shared grace window.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self.graceful_timeout, 0.0)
        while (connections or self._pending_tasks(tasks)) and loop.time() < deadline:
            await asyncio.sleep(0.1)

    async def _force_close_open_transports(self, connections: set[HttpProtocol]) -> int:
        transports = [
            protocol.transport
            for protocol in connections
            if protocol.transport and not protocol.transport.is_closing()
        ]
        for transport in transports:
            transport.close()
        if transports:
            await asyncio.sleep(0)
        return len(transports)

    def _pending_tasks(self, tasks: set[asyncio.Task[Any]]) -> list[asyncio.Task[Any]]:
        return [task for task in tasks if not task.done()]

    async def _cancel_pending_tasks(self, tasks: set[asyncio.Task[Any]]) -> int:
        pending = self._pending_tasks(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return len(pending)

    @contextmanager
    def _signal_handlers(
        self,
        loop: asyncio.AbstractEventLoop,
        shutdown_future: asyncio.Future[None],
    ) -> Iterator[None]:
        """Temporarily install SIGINT/SIGTERM handlers for graceful shutdown."""

        def on_signal() -> None:
            if shutdown_future.done():
                return
            shutdown_future.set_result(None)

        previous_handlers: dict[signal.Signals, PreviousSignalHandler] = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[sig] = signal.getsignal(sig)
                # Signal handlers can run off-loop, so we always hop back to asyncio thread-safely.
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_signal))
            except (RuntimeError, ValueError):
                continue

        try:
            yield
        finally:
            for sig, previous in previous_handlers.items():
                try:
                    signal.signal(sig, previous)
                except (RuntimeError, ValueError):
                    continue

    def _create_unix_socket(self) -> socket.socket:
        """Create and bind Unix socket."""
        assert self.unix_socket is not None
        if os.path.exists(self.unix_socket):
            st_mode = os.stat(self.unix_socket).st_mode
            if stat.S_ISSOCK(st_mode):
                os.unlink(self.unix_socket)
            else:
                raise StarioError(
                    f"Unix socket path exists and is not a socket: {self.unix_socket}",
                    help_text="Remove the file or choose a different --unix-socket path.",
                )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.bind(self.unix_socket)
        # Group-writable by default so a reverse proxy can share access without world write.
        os.chmod(self.unix_socket, self.unix_socket_mode)
        sock.listen(self.backlog)
        return sock

    def _cleanup_unix_socket(self) -> None:
        """Close and remove Unix socket resources."""
        if self._sock:
            self._sock.close()
            self._sock = None
        if self.unix_socket and os.path.exists(self.unix_socket):
            os.unlink(self.unix_socket)

    @asynccontextmanager
    async def _date_tick_scope(self) -> AsyncIterator[None]:
        """Update HTTP Date header every second."""

        async def tick_date() -> None:
            line = [b"date: ", b"", b"\r\n"]
            while True:
                now = datetime.now(timezone.utc)
                line[1] = format_datetime(now, usegmt=True).encode("ascii")
                self._date_header = b"".join(line)
                await asyncio.sleep(1)

        task = asyncio.create_task(tick_date())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _record_startup_attrs(self, span: Span) -> None:
        attrs: dict[str, str | int | float] = {
            "server.backlog": self.backlog,
            "server.graceful_timeout": self.graceful_timeout,
            "server.compression.min_size": self.compression.min_size,
            "server.compression.zstd_level": self.compression.zstd_level,
            "server.compression.brotli_level": self.compression.brotli_level,
            "server.compression.gzip_level": self.compression.gzip_level,
        }
        if self.event_loop_name is not None:
            attrs["server.event_loop"] = self.event_loop_name
        if self.compression.zstd_window_log is not None:
            attrs["server.compression.zstd_window_log"] = (
                self.compression.zstd_window_log
            )
        if self.compression.brotli_window_log is not None:
            attrs["server.compression.brotli_window_log"] = (
                self.compression.brotli_window_log
            )
        if self.compression.gzip_window_bits is not None:
            attrs["server.compression.gzip_window_bits"] = (
                self.compression.gzip_window_bits
            )
        if self.unix_socket:
            attrs["server.listen_mode"] = "unix_socket"
            attrs["server.unix_socket"] = self.unix_socket
            attrs["server.unix_socket_mode"] = oct(self.unix_socket_mode)
        else:
            attrs["server.listen_mode"] = "tcp"
            attrs["server.host"] = self.host
            attrs["server.port"] = self.port
        attrs["server.limits.max_request_header_bytes"] = self.max_request_header_bytes
        attrs["server.limits.max_request_body_bytes"] = self.max_request_body_bytes
        span.attrs(attrs)
