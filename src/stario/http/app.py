"""
Stario - Minimal async HTTP server with optional multi-threading.
"""

import asyncio
import signal
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import lru_cache
from typing import Any, Callable

from stario.exceptions import HttpException
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
    ) -> None:
        """Convenience method to create and run a server."""
        server = Server(self, host, port, workers, graceful_timeout)
        await server.run()


# =============================================================================
# Server
# =============================================================================


@dataclass
class _WorkerState:
    """Per-worker resources created during startup."""

    server: asyncio.Server
    connections: set[HttpProtocol]


@dataclass
class Server:
    """HTTP server: lifecycle and networking."""

    app: Stario
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    graceful_timeout: float = 5.0
    backlog: int = 2048

    def __post_init__(self) -> None:
        self._running = False
        self._stop: Future[None] = Future()
        self._barrier = threading.Barrier(self.workers)
        self._errors: list[Exception] = []

        # Shared across workers
        self._date_header = b""
        self._date_task: asyncio.Task[None] | None = None

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
            loop.add_signal_handler(sig, on_signal)

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
            await self._shutdown(loop, worker_id, state)

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

        # This is the only expected failure point (port in use, permission denied)
        # Always use reuse_port=True to allow clean restarts during development
        # (e.g., with watchfiles) when the old process hasn't fully released the port
        server = await loop.create_server(
            lambda: HttpProtocol(
                loop,
                self.app.handle_request,
                lambda: self._date_header,
                self.app._compression.select,
                shutdown,
                connections,
            ),
            self.host,
            self.port,
            reuse_port=True,
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

        return _WorkerState(server, connections)

    async def _shutdown(
        self, loop: asyncio.AbstractEventLoop, worker_id: int, state: _WorkerState
    ) -> None:
        """Gracefully shutdown: drain connections, close server."""

        span = self._shutdown_span
        if span:
            span[f"server.worker.{worker_id}.connections"] = len(state.connections)

        state.server.close()

        if connections := state.connections:

            # default alive() listens to disconnect and shutdown
            # so if they do so they should already know to stop
            # Give them a chance to clean up / send last message
            await asyncio.sleep(0.1)

            # Force-close remaining connections
            for conn in connections:
                if conn.transport and not conn.transport.is_closing():
                    conn.transport.close()

        await state.server.wait_closed()

        # Worker 0 stops the date header ticker
        if worker_id == 0 and self._date_task:
            self._date_task.cancel()
            try:
                await self._date_task
            except asyncio.CancelledError:
                pass

        # Drain pending tasks
        pending = [
            t
            for t in asyncio.all_tasks(loop)
            if t is not asyncio.current_task() and not t.done()
        ]
        if span:
            span[f"server.worker.{worker_id}.pending_tasks"] = len(pending)
        if pending:
            try:
                async with asyncio.timeout(self.graceful_timeout):
                    await asyncio.gather(*pending, return_exceptions=True)
            except TimeoutError:

                timedout = [t for t in pending if not t.done()]
                if span:
                    span[f"server.worker.{worker_id}.timedout_tasks"] = len(timedout)
                for t in timedout:
                    t.cancel()
                # Wait for cancellation to complete
                await asyncio.gather(*timedout, return_exceptions=True)

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
