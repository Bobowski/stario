"""
Stario - Minimal async HTTP server with optional multi-threading.
"""

import asyncio
import os
import signal
import socket
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import lru_cache
from typing import Any, Callable

from stario.exceptions import HttpException, StarioError
from stario.http.types import Context
from stario.telemetry.core import Span

from .protocol import HttpProtocol
from .request import Request
from .router import Router
from .types import ErrorHandler
from .writer import CompressionConfig, Writer

# =============================================================================
# Application
# =============================================================================


class Stario(Router):
    """HTTP application: routing and request handling."""

    def __init__(
        self,
        tracer: Callable[[str], Span],
        compression: CompressionConfig = CompressionConfig(),
    ) -> None:
        super().__init__()
        self._tracer = tracer
        self._compression = compression
        self._error_handlers: dict[type[Exception], ErrorHandler[Any]] = {
            HttpException: lambda c, w, exc: exc.respond(w),
        }

        # Host-based routing
        self._hosts_exact: dict[str, Router] = {}
        self._hosts_wildcard: list[tuple[str, Router]] = []  # (suffix, router)

        @lru_cache(maxsize=64)
        def find_handler(exc_type: type[Exception]) -> ErrorHandler[Any] | None:
            for t in exc_type.__mro__:
                if t is Exception:
                    return None
                if handler := self._error_handlers.get(t):
                    return handler
            return None

        self._find_error_handler = find_handler

    def on_error(
        self, exc_type: type[Exception], handler: ErrorHandler[Exception]
    ) -> None:
        """Register custom error handler for exception type."""
        self._error_handlers[exc_type] = handler
        self._find_error_handler.cache_clear()

    # =========================================================================
    # Host-based routing
    # =========================================================================

    def host(self, pattern: str, router: Router) -> None:
        """
        Route requests to a router based on Host header.

        Supports exact matches and wildcard prefixes:
        - "api.example.com" - exact match
        - "*.example.com" - wildcard, sets request.subhost to matched portion

        Precedence: exact hosts first, then wildcards (longest suffix first),
        then fallback to routes registered directly on the app.

        Example:
            api = Router()
            api.get("/users", list_users)
            app.host("api.example.com", api)

            tenant = Router()
            tenant.get("/dashboard", dashboard)
            app.host("*.example.com", tenant)  # request.subhost = "acme"

        Host matching is checked before path routing. Routes registered
        directly on the app act as fallback for unmatched hosts.
        """
        pattern = pattern.lower()

        # Reject bare "*" - users should use fallback routes instead
        if pattern == "*":
            raise StarioError(
                "Invalid host pattern: '*'",
                context={"pattern": pattern},
                help_text="Use '*.domain.com' for wildcard subdomains. "
                "Routes registered directly on the app serve as fallback for unmatched hosts.",
            )

        if pattern.startswith("*."):
            suffix = pattern[1:]  # "*.example.com" -> ".example.com"
            # Check for duplicate wildcard
            for existing_suffix, _ in self._hosts_wildcard:
                if existing_suffix == suffix:
                    raise StarioError(
                        f"Wildcard host already registered: {pattern}",
                        context={"pattern": pattern},
                        help_text="Each wildcard pattern can only have one router.",
                    )
            self._hosts_wildcard.append((suffix, router))
            # Longest suffix first for most specific match
            self._hosts_wildcard.sort(key=lambda x: len(x[0]), reverse=True)
        else:
            if pattern in self._hosts_exact:
                raise StarioError(
                    f"Host already registered: {pattern}",
                    context={"pattern": pattern},
                    help_text="Each host pattern can only have one router.",
                )
            self._hosts_exact[pattern] = router

    async def dispatch(self, c: Context, w: Writer) -> None:
        """Dispatch request, checking host routing first."""
        # Fast path: skip if no host routing configured
        if self._hosts_exact or self._hosts_wildcard:
            host = c.req.host

            # O(1) exact match
            if router := self._hosts_exact.get(host):
                await router.dispatch(c, w)
                return

            # Wildcard match (typically 1-3 patterns)
            for suffix, router in self._hosts_wildcard:
                if host.endswith(suffix):
                    c.req.subhost = host[: -len(suffix)]
                    await router.dispatch(c, w)
                    return

        # Fallback to regular path routing
        await Router.dispatch(self, c, w)

    async def handle_request(self, req: Request, w: Writer) -> None:
        """Handle request with tracing and error handling."""
        span = self._tracer(req.method)
        span["request.method"] = req.method
        span["request.path"] = req.path
        c = Context(app=self, req=req, span=span, state={})

        try:
            await self.dispatch(c, w)
        except Exception as exc:
            handled = False
            if not w.started:
                if handler := self._find_error_handler(type(exc)):
                    try:
                        result = handler(c, w, exc)
                        if asyncio.iscoroutine(result):
                            await result
                        handled = True
                    except Exception:
                        pass
                if not handled:
                    w.text("Internal Server Error", 500)
            if not handled:
                span.error = str(exc)
                span(exc)
        finally:
            w.end()
            span["response.status_code"] = w._status_code
            span.end()

    async def serve(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        graceful_timeout: float = 5.0,
        workers: int = 1,
        unix_socket: str | None = None,
    ) -> None:
        """Convenience method to create and run a server."""
        server = Server(
            self,
            host=host,
            port=port,
            workers=workers,
            graceful_timeout=graceful_timeout,
            unix_socket=unix_socket,
        )
        await server.run()


# =============================================================================
# Server
# =============================================================================


@dataclass
class _WorkerState:
    """Per-worker resources created during startup."""

    server: asyncio.Server
    connections: set[HttpProtocol]
    tasks: set[asyncio.Task[None]]


@dataclass
class Server:
    """HTTP server: lifecycle and networking."""

    app: Stario
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    graceful_timeout: float = 5.0
    backlog: int = 2048
    unix_socket: str | None = None

    def __post_init__(self) -> None:
        # Multiple TCP workers require SO_REUSEPORT to bind to the same port
        if (
            self.workers > 1
            and not self.unix_socket
            and not hasattr(socket, "SO_REUSEPORT")
        ):
            raise StarioError(
                f"Cannot use {self.workers} workers (SO_REUSEPORT unavailable)",
                help_text="Multiple workers require SO_REUSEPORT to bind to the same port. "
                "Use workers=1 on Windows, or run on Linux/macOS for multi-worker support.",
            )

        self._running = False
        self._stop: Future[None] = Future()
        self._barrier = threading.Barrier(self.workers)
        self._errors: list[Exception] = []

        # Shared across workers
        self._date_header = b""
        self._date_task: asyncio.Task[None] | None = None
        # Pre-created socket (Unix multi-worker)
        self._sock: socket.socket | None = None

        # Telemetry spans
        self._startup_span: Span | None = None
        self._shutdown_span: Span | None = None

    async def run(self) -> None:
        """
        Run server until shutdown signal.

        Raises:
            RuntimeError: If this server instance is already running
            Exception: First startup error from any worker
        """
        if self._running:
            from stario.exceptions import StarioError

            raise StarioError(
                "Server already running",
                help_text="Create a new Server instance to run multiple servers.",
            )
        self._running = True

        loop = asyncio.get_running_loop()
        span = self._startup_span = self.app._tracer("server.startup")
        if self.unix_socket:
            span["server.unix_socket"] = self.unix_socket
        else:
            span["server.host"] = self.host
            span["server.port"] = self.port
        span["server.workers"] = self.workers

        def on_signal() -> None:
            if not self._stop.done():
                span = self._shutdown_span = self.app._tracer("server.shutdown")
                span["server.graceful_timeout"] = self.graceful_timeout
                span["server.workers"] = self.workers
                self._stop.set_result(None)

        for sig in (signal.SIGINT, signal.SIGTERM):
            # Compatible with Unix + Windows: schedule shutdown on the running loop.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_signal))

        # Unix sockets: create once, share across worker threads
        if self.unix_socket:
            self._sock = self._create_unix_socket()

        threads = [
            threading.Thread(target=self._thread, args=(i,), daemon=True)
            for i in range(1, self.workers)
        ]
        for t in threads:
            t.start()

        try:
            await self._serve(loop, worker_id=0)
        finally:
            for t in threads:
                t.join(timeout=self.graceful_timeout + 5)

            # Clean up Unix socket on failure (normal shutdown handled in _shutdown)
            if self._errors:
                self._cleanup_unix_socket()

            span.end()
            if shutdown := self._shutdown_span:
                shutdown.end()

            if self._errors:
                raise self._errors[0]

    def _thread(self, worker_id: int) -> None:
        """Worker thread entry point."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._serve(loop, worker_id))
        loop.close()

    async def _serve(self, loop: asyncio.AbstractEventLoop, worker_id: int) -> None:
        """Worker lifecycle: startup → wait for stop → shutdown."""
        try:
            shutdown = asyncio.wrap_future(self._stop)
            state = await self._startup(loop, worker_id, shutdown)
            await shutdown  # Wait for shutdown signal
            await self._shutdown(worker_id, state)

        except threading.BrokenBarrierError:
            pass  # Another worker failed - they logged the error

        except Exception as e:
            self._errors.append(e)
            if self._startup_span:
                self._startup_span(e)
            try:
                self._barrier.abort()
            except threading.BrokenBarrierError:
                pass

    async def _startup(
        self,
        loop: asyncio.AbstractEventLoop,
        worker_id: int,
        shutdown: asyncio.Future,
    ) -> _WorkerState:
        """Create server, sync with other workers, start accepting."""
        connections: set[HttpProtocol] = set()
        tasks: set[asyncio.Task[None]] = set()

        def protocol_factory() -> HttpProtocol:
            return HttpProtocol(
                loop,
                self.app.handle_request,
                lambda: self._date_header,
                self.app._compression.select,
                shutdown,
                connections,
                tasks,
            )

        # This is the only expected failure point (port in use, permission denied)
        if self._sock:
            # Unix socket: all workers share the same pre-created socket
            server = await loop.create_unix_server(
                protocol_factory,
                sock=self._sock,
                start_serving=False,
            )
        else:
            # TCP: use reuse_port when multiple workers need to bind to the same port
            # (SO_REUSEPORT availability is validated in __post_init__ when workers > 1)
            server = await loop.create_server(
                protocol_factory,
                self.host,
                self.port,
                reuse_port=self.workers > 1,
                backlog=self.backlog,
                start_serving=False,
            )

        # Sync - if another worker failed, this raises BrokenBarrierError
        try:
            self._barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            server.close()
            await server.wait_closed()
            raise

        # Worker 0 starts the date header ticker
        if worker_id == 0:
            self._date_task = loop.create_task(self._tick_date())
            if self._startup_span:
                self._startup_span.end()

        await server.start_serving()

        return _WorkerState(server, connections, tasks)

    async def _shutdown(self, worker_id: int, state: _WorkerState) -> None:
        """Gracefully shutdown: close connections, cancel handlers, clean up.

        Shutdown sequence:
        1. Stop accepting new connections
        2. Wait up to graceful_timeout for handlers to finish
        3. Force-close remaining connections
        4. Cancel any handler tasks still running
        5. Clean up server resources (unix socket, date tick)

        Only stario-managed handler tasks are cancelled. External tasks on
        the same event loop are never touched, so periodic flushes, metrics
        collectors, etc. keep running normally.
        """

        # Stop accepting new connections
        state.server.close()

        # alive() listens to the shutdown future, so handlers already
        # know to stop. Give them graceful_timeout to finish on their
        # own (send final SSE event, complete in-flight response, etc.)
        open_connections = len(state.connections)
        for _ in range(int(self.graceful_timeout / 0.1)):
            # Check each 0.1 seconds if the connections are closed
            if not state.connections:
                break

            await asyncio.sleep(0.1)

        # Force-close remaining connections
        remaining = [
            c.transport
            for c in state.connections
            if c.transport and not c.transport.is_closing()
        ]
        for transport in remaining:
            transport.close()

        # Cancel handler tasks still running after connections closed
        stuck = [t for t in state.tasks if not t.done()]
        for t in stuck:
            t.cancel()
        if stuck:
            await asyncio.gather(*stuck, return_exceptions=True)

        # Log the shutdown metrics
        if span := self._shutdown_span:
            w = f"server.worker.{worker_id}"
            span[f"{w}.connections_at_shutdown"] = open_connections
            span[f"{w}.connections_force_closed"] = len(remaining)
            span[f"{w}.handler_tasks_cancelled"] = len(stuck)

        await state.server.wait_closed()

        # Worker 0 cleans up Unix socket and stops the date header ticker
        if worker_id == 0:
            self._cleanup_unix_socket()
            await self._cleanup_date_tick()

    def _create_unix_socket(self) -> socket.socket:
        """Create and bind Unix socket (called once, shared by all workers)."""
        assert self.unix_socket
        if os.path.exists(self.unix_socket):
            os.unlink(self.unix_socket)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.bind(self.unix_socket)
        os.chmod(self.unix_socket, 0o666)
        sock.listen(self.backlog)
        return sock

    def _cleanup_unix_socket(self) -> None:
        """Close socket and remove socket file."""
        if self._sock:
            self._sock.close()
            self._sock = None
        if self.unix_socket and os.path.exists(self.unix_socket):
            os.unlink(self.unix_socket)

    async def _cleanup_date_tick(self) -> None:
        """Cancel the date header ticker task."""
        if not self._date_task:
            return

        self._date_task.cancel()
        try:
            await self._date_task
        except asyncio.CancelledError:
            pass
        self._date_task = None

    async def _tick_date(self) -> None:
        """
        Update HTTP Date header every second (run by worker 0 only).

        Why a shared date header updated by a single worker?
        - HTTP/1.1 requires a Date header on every response
        - Formatting timestamps is expensive (format_datetime + encode)
        - All workers share _date_header via the Server instance
        - 1-second granularity is plenty accurate for HTTP caching semantics

        Why worker 0 only?
        - Only one task needs to update the shared value
        - Reduces CPU overhead in multi-worker deployments
        - Worker 0 always exists (even with workers=1)
        """
        line = [b"date: ", b"", b"\r\n"]
        while True:
            now = datetime.now(timezone.utc)
            line[1] = format_datetime(now, usegmt=True).encode()
            self._date_header = b"".join(line)
            await asyncio.sleep(1)
