"""
Runs `App` behind an asyncio listener: signal handling, bootstrap, graceful drain, then socket teardown.

Bootstrap startup completes before `start_serving`; exceptions there fail startup loudly. Transport policy
(TCP vs Unix, backlog, compression defaults) lives here so `Router`/`App` stay free of process-level concerns.
"""

import asyncio
import importlib
import os
import signal
import socket
import stat
import sys
from collections.abc import AsyncGenerator, Callable, Coroutine, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime
from email.utils import format_datetime
from types import FrameType
from typing import Any, Literal

from stario.exceptions import StarioError
from stario.telemetry.core import Span, Tracer
from stario.telemetry.spans import ProxySpan

from .app import App
from .bootstrap import (
    Bootstrap,
    ShutdownTrigger,
    bootstrap_run,
)
from .config import ServerConfig
from .protocol import HttpProtocol

type SignalHandler = Callable[[int, FrameType | None], object]
type PreviousSignalHandler = signal.Handlers | int | SignalHandler | None

type LoopRun[T] = Callable[[Coroutine[Any, Any, T]], T]

# Upper bound on the force-close loop after the graceful wait (see _drain_listener).
_FORCE_CLOSE_CAP = 1.0

# Yield to the event loop this many times while waiting for connection_made to register.
_ACCEPT_REGISTER_YIELDS = 10


def resolve_loop_runner(event_loop: Literal["asyncio", "uvloop"]) -> LoopRun[Any]:
    """Return `asyncio.run` or `uvloop.run` for the configured event loop."""
    if event_loop == "asyncio":
        return asyncio.run
    if sys.platform == "win32":
        raise StarioError(
            "uvloop is not supported on Windows",
            help_text="Set event_loop='asyncio' in ServerConfig.",
        )
    try:
        uvloop = importlib.import_module("uvloop")
    except ImportError as exc:
        raise StarioError(
            "uvloop is not installed",
            help_text="Install uvloop or set event_loop='asyncio' in ServerConfig.",
        ) from exc
    run = getattr(uvloop, "run", None)
    if run is None:
        raise StarioError(
            "uvloop does not expose run()",
            help_text="Set event_loop='asyncio' in ServerConfig.",
        )
    return run


class Server:
    """Binds a listener, runs bootstrap on a fresh app, serves until SIGINT/SIGTERM, then drains.

    One instance, one `serve()` / `run()` — create a new `Server` to restart.

    Typical embedding::

        with tracer:
            Server(bootstrap, tracer, config=config).run()
    """

    def __init__(
        self,
        bootstrap: Bootstrap,
        tracer: Tracer,
        *,
        config: ServerConfig | None = None,
    ) -> None:
        """Configure listening, bootstrap, telemetry, and per-connection compression.

        - `bootstrap`: Async generator `(app, span)` with a single `yield`.
        - `tracer`: Telemetry backend implementing the `Tracer` protocol.
        - `config`: Listen address, limits, compression, shutdown policy, and event loop.
        """

        self.bootstrap = bootstrap
        self.config = config if config is not None else ServerConfig()
        self.tracer = tracer

        self._used = False
        self._date_header = b""
        self._urgent_drain = False

    def run(self) -> None:
        """Block until shutdown; picks the event loop from `config.event_loop`.

        The tracer must already be entered by the caller (see `cli/runtime.py`).
        """
        resolve_loop_runner(self.config.event_loop)(self.serve())

    async def serve(self) -> None:
        """Run until SIGINT/SIGTERM (or fatal error); requires a running event loop.

        The tracer must already be entered by the caller (see `cli/runtime.py`).
        """
        if self._used:
            raise StarioError(
                "Server instance already used",
                help_text="Create a new Server instance to run again.",
            )
        self._used = True

        app = App()
        span = self._open_startup_span()  # ProxySpan: startup → shutdown via replace()

        # Signal handlers stay active through span.end(); see _signal_handlers.
        with (
            self._unix_listen_socket() as listen_sock,
            self._signal_handlers(app.shutdown),
        ):
            try:
                async with (
                    bootstrap_run(self.bootstrap, app, span),
                    self._date_tick(),
                    self._listener(listen_sock, app, span),
                ):
                    # Blocks until a signal (or test code) completes app.shutdown.
                    await app.shutdown
            except BaseException as exc:
                span.exception(exc)
                span.fail(str(exc))
                raise
            finally:
                # Safety net: unblock shutdown waiters when serve ends without a signal.
                app.signal_shutdown()

                # Ends whichever span ProxySpan currently points at (usually shutdown).
                span.end()

    def _open_startup_span(self) -> ProxySpan:
        span = self.tracer.create("server.startup")
        span.start()
        self._record_startup_attrs(span)
        return ProxySpan(span)

    def _open_shutdown_span(self, span: ProxySpan, trigger: ShutdownTrigger) -> None:
        # Swap startup → shutdown on the same handle so serve() can span.end() once.
        startup_id = span.id
        shutdown = self.tracer.create("server.shutdown")
        shutdown.link("server.startup", startup_id)
        shutdown.attr("server.shutdown.trigger", trigger)
        shutdown.start()
        span.replace(shutdown)

    async def _create_listener(
        self,
        listen_sock: socket.socket | None,
        app: App,
        connections: set[HttpProtocol],
    ) -> asyncio.Server:
        loop = asyncio.get_running_loop()

        def protocol_factory() -> HttpProtocol:
            return HttpProtocol(
                loop,
                app,
                self.tracer,
                lambda: (
                    self._date_header
                ),  # callable: reads refreshed bytes each response
                self.config.compression,
                connections,  # shared set; protocol adds/removes self on connect/lost
                self.config.requests,
            )

        if listen_sock is not None:
            return await loop.create_unix_server(protocol_factory, sock=listen_sock)
        return await loop.create_server(
            protocol_factory,
            self.config.host,
            self.config.port,
            backlog=self.config.backlog,
            reuse_address=self.config.reuse_addr,
        )

    async def _drain_listener(
        self,
        server: asyncio.Server,
        app: App,
        connections: set[HttpProtocol],
        span: ProxySpan | None = None,
    ) -> None:
        """Stop accepting, drain in-flight work, then tear down transports and tasks.

        Shutdown proceeds in ordered phases:

          0. Idle keep-alive sockets (no in-flight handler) close immediately.
          1. Graceful wait — let handlers finish and remaining connections close.
          2. Force-close loop — cap `min(timeout, _FORCE_CLOSE_CAP)` for stuck transports.

        Total wall time can exceed the config value by up to `_FORCE_CLOSE_CAP` seconds.
        """
        loop = asyncio.get_running_loop()

        # --- stop accepting ---------------------------------------------------
        server.close()

        # connection_made may not have run yet; yield so open count is accurate.
        for _ in range(_ACCEPT_REGISTER_YIELDS):
            if connections:
                break
            await asyncio.sleep(0)

        open_connections = len(connections)

        # Idle keep-alive sockets are not tied to app.tasks; close them now.
        idle_closed = sum(
            1 for protocol in list(connections) if protocol.close_if_idle()
        )
        if idle_closed:
            await asyncio.sleep(0)

        # --- phase 1: graceful wait (full graceful_shutdown_timeout) --------
        await self._wait_for_managed_work_to_drain(connections, app.tasks)

        # --- phase 2: force-close stuck transports ----------------------------
        force_closed = await self._force_close_open_transports(connections)
        force_close_budget = min(
            max(self.config.graceful_shutdown_timeout, 0.0), _FORCE_CLOSE_CAP
        )
        close_deadline = loop.time() + force_close_budget
        while (
            connections
            and not self._urgent_drain
            and loop.time() < close_deadline
        ):
            force_closed += await self._force_close_open_transports(connections)
            await asyncio.sleep(0)

        # --- phase 3: cancel orphaned app.create_task work ------------------
        cancelled_tasks = await self._cancel_pending_tasks(app.tasks)

        # Protocols with transport=None never leave the set; count for telemetry.
        stale_connections = len(connections)

        if span is not None:
            span.attrs(
                {
                    "server.shutdown.open_connections": open_connections,
                    "server.shutdown.urgent": self._urgent_drain,
                    "server.shutdown.idle_closed": idle_closed,
                    "server.shutdown.force_closed": force_closed,
                    "server.shutdown.stale_connections": stale_connections,
                    "server.shutdown.cancelled_tasks": cancelled_tasks,
                }
            )
            span.event("server.shutdown.closed")

        await server.wait_closed()

    async def _wait_for_managed_work_to_drain(
        self,
        connections: set[HttpProtocol],
        tasks: set[asyncio.Task[Any]],
    ) -> None:
        # Wait until no open connections and no pending app.create_task work, or timeout.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self.config.graceful_shutdown_timeout, 0.0)

        while not self._urgent_drain and loop.time() < deadline:
            pending = self._pending_tasks(tasks)
            if not connections and not pending:
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            timeout = min(0.05, remaining)

            if pending:
                await asyncio.wait(
                    pending,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(timeout)

    async def _force_close_open_transports(self, connections: set[HttpProtocol]) -> int:
        transports = [
            protocol.transport
            for protocol in connections
            if protocol.transport and not protocol.transport.is_closing()
        ]
        for transport in transports:
            transport.close()
        if transports:
            # Let connection_lost run so the shared connections set updates.
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
        shutdown_future: asyncio.Future[None],
    ) -> Generator[None]:
        """Install SIGINT/SIGTERM handlers for graceful shutdown.

        Contract:
          - 1st signal → complete ``shutdown_future`` and start drain
          - 2nd signal → set ``_urgent_drain`` (skip remaining graceful wait)
          - after shutdown started → ``SIG_IGN`` until exit so extra signals
            during tracer flush do not become ``KeyboardInterrupt``
        """
        loop = asyncio.get_running_loop()

        def on_signal() -> None:
            if shutdown_future.done():
                self._urgent_drain = True
                return
            shutdown_future.set_result(None)

        previous_handlers: dict[signal.Signals, PreviousSignalHandler] = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[sig] = signal.getsignal(sig)

                def _on_signal(_signum: int, _frame: FrameType | None) -> None:
                    loop.call_soon_threadsafe(on_signal)

                signal.signal(sig, _on_signal)
            except RuntimeError, ValueError:
                continue

        try:
            yield
        finally:
            for sig, previous in previous_handlers.items():
                try:
                    if shutdown_future.done():
                        signal.signal(sig, signal.SIG_IGN)
                    else:
                        signal.signal(sig, previous)
                except RuntimeError, ValueError:
                    continue

    @contextmanager
    def _unix_listen_socket(self) -> Generator[socket.socket | None]:
        """Bind a Unix listen socket, or yield `None` for TCP listen."""
        path = self.config.unix_socket
        if path is None:
            yield None
            return

        if os.path.exists(path):
            st_mode = os.stat(path).st_mode
            if stat.S_ISSOCK(st_mode):
                os.unlink(path)
            else:
                raise StarioError(
                    f"Unix socket path exists and is not a socket: {path}",
                    help_text="Remove the file or choose a different STARIO_UNIX_SOCKET path.",
                )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        bound_file_id: tuple[int, int] | None = None
        try:
            sock.bind(path)
            bound_stat = os.stat(path)
            bound_file_id = (
                bound_stat.st_dev,
                bound_stat.st_ino,
            )  # unlink only our inode
            os.chmod(path, self.config.unix_socket_mode)
            sock.listen(self.config.backlog)
            yield sock
        finally:
            sock.close()
            if bound_file_id is not None:
                try:
                    current_stat = os.stat(path)
                except FileNotFoundError:
                    pass
                else:
                    # Skip unlink if another process rebound the path after we closed.
                    if (
                        stat.S_ISSOCK(current_stat.st_mode)
                        and (current_stat.st_dev, current_stat.st_ino) == bound_file_id
                    ):
                        os.unlink(path)

    @asynccontextmanager
    async def _listener(
        self,
        listen_sock: socket.socket | None,
        app: App,
        span: ProxySpan,
    ) -> AsyncGenerator[asyncio.Server]:
        """Bind on enter; drain in-flight work on exit."""
        connections: set[HttpProtocol] = set()
        listener = await self._create_listener(listen_sock, app, connections)

        # Startup span ends once we are listening; shutdown span opens on exit.
        span.attr("server.listening", True)
        span.end()
        try:
            yield listener
        except BaseException:
            self._open_shutdown_span(span, "runtime_failure")
            raise
        else:
            self._open_shutdown_span(span, "expected_stop")  # signal or app.shutdown
        finally:
            await self._drain_listener(listener, app, connections, span)

    @asynccontextmanager
    async def _date_tick(self) -> AsyncGenerator[None]:
        """Refresh the shared Date header now, then once per second until exit."""

        def refresh() -> None:
            now = datetime.now(UTC)
            # Preformatted wire bytes; Writer concatenates without per-response format_datetime.
            self._date_header = b"date: %s\r\n" % format_datetime(
                now, usegmt=True
            ).encode("ascii")

        async def tick() -> None:
            while True:
                await asyncio.sleep(1)
                refresh()

        refresh()  # first value before any connection can read it
        task = asyncio.create_task(tick())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _record_startup_attrs(self, span: Span) -> None:
        attrs: dict[str, str | int | float] = {
            "server.backlog": self.config.backlog,
            "server.graceful_shutdown_timeout": self.config.graceful_shutdown_timeout,
            "server.compression.min_size": self.config.compression.min_size,
            "server.compression.zstd_level": self.config.compression.zstd_level,
            "server.compression.brotli_level": self.config.compression.brotli_level,
            "server.compression.gzip_level": self.config.compression.gzip_level,
            "server.timeout.request_header": self.config.requests.header_timeout,
            "server.timeout.request_body": self.config.requests.body_timeout,
            "server.timeout.keep_alive": self.config.requests.keep_alive_timeout,
            "server.event_loop": self.config.event_loop,
        }
        if self.config.compression.zstd_window_log is not None:
            attrs["server.compression.zstd_window_log"] = (
                self.config.compression.zstd_window_log
            )
        if self.config.compression.brotli_window_log is not None:
            attrs["server.compression.brotli_window_log"] = (
                self.config.compression.brotli_window_log
            )
        if self.config.compression.gzip_window_bits is not None:
            attrs["server.compression.gzip_window_bits"] = (
                self.config.compression.gzip_window_bits
            )
        if self.config.unix_socket:
            attrs["server.listen_mode"] = "unix_socket"
            attrs["server.unix_socket"] = self.config.unix_socket
            attrs["server.unix_socket_mode"] = oct(self.config.unix_socket_mode)
        else:
            attrs["server.listen_mode"] = "tcp"
            attrs["server.host"] = self.config.host
            attrs["server.port"] = self.config.port
            attrs["server.reuse_addr"] = self.config.reuse_addr
        attrs["server.limits.max_request_header_bytes"] = (
            self.config.requests.max_header_bytes
        )
        attrs["server.limits.max_request_body_bytes"] = (
            self.config.requests.max_body_bytes
        )
        span.attrs(attrs)
