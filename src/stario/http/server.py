"""
Runs `App` behind an asyncio listener: signal handling, bootstrap, graceful drain, then socket teardown.

Bootstrap startup completes before `start_serving`; exceptions there fail startup loudly. Transport policy
(TCP vs Unix, backlog, compression defaults) lives here so `Router`/`App` stay free of process-level concerns.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import signal
import socket
import stat
import sys
import sysconfig
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime
from email.utils import format_datetime
from types import FrameType
from typing import Any

from stario_cython.protocol import HttpProtocol

from stario._env import env_bool
from stario.exceptions import StarioError
from stario.telemetry.core import Span, Tracer
from stario.telemetry.spans import ProxySpan

from .app import App
from .bootstrap import (
    Bootstrap,
    ShutdownTrigger,
    bootstrap_run,
)
from .compression import CompressionConfig
from .config import EventLoopKind, RequestPolicy, ServerConfig

type Connection = Any
type ProtocolMaker = Callable[
    [
        asyncio.AbstractEventLoop,
        App,
        Tracer,
        list[bytes],
        CompressionConfig,
        set[Connection],
        RequestPolicy,
    ],
    asyncio.Protocol,
]


def _make_http_protocol(
    loop: asyncio.AbstractEventLoop,
    app: App,
    tracer: Tracer,
    date_box: list[bytes],
    compression: CompressionConfig,
    connections: set[Connection],
    requests: RequestPolicy,
) -> asyncio.Protocol:
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


type SignalHandler = Callable[[int, FrameType | None], object]
type PreviousSignalHandler = signal.Handlers | int | SignalHandler | None

type LoopRun[T] = Callable[[Coroutine[Any, Any, T]], T]

# Upper bound on the force-close loop after the graceful wait (see _drain_listener).
_FORCE_CLOSE_CAP = 1.0

# Keep in sync with ``stario_cython.protocol``: Cython skips its own sweeper
# task when the Date tick already walks connections once a second.
_DATE_TICK_SWEEPS_TIMEOUTS = "_stario_date_tick_sweeps_timeouts"

# Yield to the event loop this many times while waiting for connection_made to register.
_ACCEPT_REGISTER_YIELDS = 10


def loop_implementation(
    loop: asyncio.AbstractEventLoop | None = None,
) -> EventLoopKind:
    """Classify the running (or given) loop as `asyncio` or `uvloop`."""
    if loop is None:
        loop = asyncio.get_running_loop()
    module = type(loop).__module__
    if module == "uvloop" or module.startswith("uvloop."):
        return "uvloop"
    return "asyncio"


def require_configured_loop(
    event_loop: EventLoopKind,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    where: str,
) -> EventLoopKind:
    """Refuse to continue when this thread's loop is not `STARIO_LOOP`."""
    actual = loop_implementation(loop)
    if actual != event_loop:
        raise StarioError(
            f"{where} is running {actual}, configured {event_loop}",
            help_text=(
                "Every worker uses Server.loop_runner() from STARIO_LOOP. "
                "Do not install a different event loop policy in the process."
            ),
        )
    return actual


def _stdlib_loop_factory() -> asyncio.AbstractEventLoop:
    """Stdlib loop, ignoring a process-wide uvloop policy."""
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.SelectorEventLoop()


def _asyncio_run[T](main: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(main, loop_factory=_stdlib_loop_factory)


def resolve_loop_runner(event_loop: EventLoopKind) -> LoopRun[Any]:
    """Return the runner that constructs `event_loop` on the calling thread.

    `asyncio` always uses a stdlib loop factory so `uvloop.install()` cannot
    leak into workers. `uvloop` is `uvloop.run`.
    """
    if event_loop == "asyncio":
        return _asyncio_run
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


def gil_is_enabled() -> bool:
    """True when this process cannot run Python bytecode in parallel."""
    check = getattr(sys, "_is_gil_enabled", None)
    if check is None:
        return True
    return bool(check())


def require_thread_workers(threads: int) -> None:
    """Refuse `threads>1` when the GIL is on, unless `STARIO_THREADS_ALLOW_GIL=1`."""
    if threads <= 1:
        return
    if not gil_is_enabled():
        return
    if env_bool("STARIO_THREADS_ALLOW_GIL", False):
        return
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        raise StarioError(
            "STARIO_THREADS>1 needs the GIL to stay disabled",
            help_text=(
                "An extension re-enabled the GIL. Use free-threaded wheels "
                "or set STARIO_THREADS=1."
            ),
        )
    raise StarioError(
        "STARIO_THREADS>1 requires free-threaded Python (3.14t)",
        help_text="Run a 3.14t interpreter or set STARIO_THREADS=1.",
    )


class _LoopWorker:
    """One OS thread, one event loop, connection-affine HTTP protocols."""

    def __init__(
        self,
        *,
        index: int,
        app: App,
        server: Server,
        loop_run: LoopRun[Any],
        body: Callable[[_LoopWorker], Coroutine[Any, Any, None]],
    ) -> None:
        self.index = index
        self.app = app
        self.server = server
        self.loop_run = loop_run
        self._body = body
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_kind: EventLoopKind | None = None
        self.connections: set[Connection] = set()
        self.date_box: list[bytes] = [b""]
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"stario-worker-{index}",
        )

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def submit(self, sock: socket.socket) -> None:
        loop = self.loop
        if loop is None or loop.is_closed() or self.app.shutting_down:
            sock.close()
            return
        try:
            loop.call_soon_threadsafe(self._begin, sock)
        except RuntimeError:
            sock.close()

    def _run(self) -> None:
        try:
            self.loop_run(self._body(self))
        except BaseException as exc:
            self.error = exc
            with suppress(Exception):
                self.app.signal_shutdown()
        finally:
            self.ready.set()

    def _begin(self, sock: socket.socket) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            sock.close()
            return
        loop.create_task(self._attach(sock), name="stario.worker.accept")

    async def _attach(self, sock: socket.socket) -> None:
        loop = self.loop
        assert loop is not None
        try:
            await loop.connect_accepted_socket(
                self._protocol,
                sock,
                ssl=self.server.config.ssl,
            )
        except asyncio.CancelledError:
            with suppress(OSError):
                sock.close()
            raise
        except Exception:
            with suppress(OSError):
                sock.close()

    def _protocol(self) -> asyncio.Protocol:
        loop = self.loop
        assert loop is not None
        factory = (
            self.server.make_protocol
            if self.server.make_protocol is not None
            else _make_http_protocol
        )
        return factory(
            loop,
            self.app,
            self.server.tracer,
            self.date_box,
            self.server.config.compression,
            self.connections,
            self.server.config.requests,
        )


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
        make_protocol: ProtocolMaker | None = None,
    ) -> None:
        """Configure listening, bootstrap, telemetry, and per-connection compression.

        - `bootstrap`: Async generator `(app, span)` with a single `yield`.
        - `tracer`: Telemetry backend implementing the `Tracer` protocol.
        - `config`: Listen address, limits, compression, shutdown policy, and event loop.
        - `make_protocol`: Optional factory; default is the Cython HTTP protocol.
        """

        self.bootstrap = bootstrap
        self.config = config if config is not None else ServerConfig()
        self.tracer = tracer
        self.make_protocol = make_protocol

        self._used = False
        self._date_box = [b""]
        self._urgent_drain = False
        self._live_connections: set[Connection] | None = None
        self._loop_run: LoopRun[Any] | None = None
        self._owns_loop = False

    def loop_runner(self) -> LoopRun[Any]:
        """Event-loop runner for this process (`asyncio` stdlib or `uvloop.run`).

        `run()` and every worker thread use this same callable so `STARIO_LOOP`
        is forwarded, not re-detected per thread. asyncio workers do not inherit
        a process-wide uvloop policy.
        """
        if self._loop_run is None:
            self._loop_run = resolve_loop_runner(self.config.event_loop)
        return self._loop_run

    def run(self) -> None:
        """Block until shutdown; picks the event loop from `config.event_loop`.

        The tracer must already be entered by the caller (see `cli/runtime.py`).
        """
        self._owns_loop = True
        self.loop_runner()(self.serve())

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

        if self._owns_loop:
            require_configured_loop(self.config.event_loop, where="Server.run")

        if self.config.threads > 1:
            require_thread_workers(self.config.threads)
            # Resolve once on this thread; every worker calls this same runner
            # instead of re-detecting STARIO_LOOP.
            self.loop_runner()

        app = App()
        span = self._open_startup_span()  # ProxySpan: startup → shutdown via replace()

        # Signal handlers stay active through span.end(); see _signal_handlers.
        with (
            self._listen_socket() as listen_sock,
            self._signal_handlers(app),
        ):
            try:
                if self.config.threads > 1:
                    async with bootstrap_run(self.bootstrap, app, span):
                        await self._serve_threaded(listen_sock, app, span)
                else:
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
        connections: set[Connection],
    ) -> asyncio.Server:
        loop = asyncio.get_running_loop()
        make_protocol = self.make_protocol

        def protocol_factory() -> asyncio.Protocol:
            factory = (
                make_protocol if make_protocol is not None else _make_http_protocol
            )
            return factory(
                loop,
                app,
                self.tracer,
                self._date_box,
                self.config.compression,
                connections,
                self.config.requests,
            )

        ssl_ctx = self.config.ssl
        if listen_sock is not None:
            return await loop.create_unix_server(
                protocol_factory, sock=listen_sock, ssl=ssl_ctx
            )
        return await loop.create_server(
            protocol_factory,
            self.config.host,
            self.config.port,
            backlog=self.config.backlog,
            reuse_address=self.config.reuse_addr,
            ssl=ssl_ctx,
        )

    async def _drain_listener(
        self,
        server: asyncio.Server,
        app: App,
        connections: set[Connection],
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
        while connections and not self._urgent_drain and loop.time() < close_deadline:
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
        connections: set[Connection],
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

    async def _force_close_open_transports(self, connections: set[Connection]) -> int:
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
    def _listen_socket(self) -> Generator[socket.socket | None]:
        """Unix or (when `threads>1`) TCP listen socket. `None` = N=1 TCP via create_server."""
        if self.config.unix_socket is not None:
            with self._unix_listen_socket() as sock:
                yield sock
            return
        if self.config.threads <= 1:
            yield None
            return
        sock = self._tcp_listen_socket()
        try:
            yield sock
        finally:
            sock.close()

    def _tcp_listen_socket(self) -> socket.socket:
        """Bind a TCP listener for the acceptor thread (`threads>1`)."""
        infos = socket.getaddrinfo(
            self.config.host,
            self.config.port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            flags=socket.AI_PASSIVE,
        )
        if not infos:
            raise StarioError(
                f"could not resolve listen address {self.config.host!r}",
                help_text="Set STARIO_HOST to a bindable address.",
            )
        family, socktype, proto, _canon, sockaddr = infos[0]
        sock = socket.socket(family, socktype, proto)
        if self.config.reuse_addr:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(sockaddr)
        sock.listen(self.config.backlog)
        sock.setblocking(False)
        return sock

    @contextmanager
    def _signal_handlers(
        self,
        app: App,
    ) -> Generator[None]:
        """Install SIGINT/SIGTERM handlers for graceful shutdown.

        Contract:
          - 1st signal → `app.signal_shutdown()` and start drain
          - 2nd signal → set `_urgent_drain` (skip remaining graceful wait)
          - after shutdown started → `SIG_IGN` until exit so extra signals
            during tracer flush do not become `KeyboardInterrupt`
        """
        loop = asyncio.get_running_loop()

        def on_signal() -> None:
            if app.shutting_down:
                self._urgent_drain = True
                return
            app.signal_shutdown()

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
                    if app.shutting_down:
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
        connections: set[Connection] = set()
        self._live_connections = connections
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
            self._live_connections = None

    def _sweep_connection_timeouts(
        self,
        loop: asyncio.AbstractEventLoop,
        connections: set[Connection] | None = None,
    ) -> None:
        """Compare stored deadlines to one ``loop.time()`` (Date-tick cadence)."""
        live = self._live_connections if connections is None else connections
        if not live:
            return
        now = loop.time()
        for proto in tuple(live):
            check = getattr(proto, "check_timeouts", None)
            if check is None:
                continue
            try:
                check(now)
            except Exception:
                continue

    @asynccontextmanager
    async def _date_tick(
        self,
        date_box: list[bytes] | None = None,
        connections: set[Connection] | None = None,
    ) -> AsyncGenerator[None]:
        """Refresh Date, then once per second also sweep connection timeouts.

        Header/idle/body-stall defaults are 5s/5s/30s. One-second granularity
        matches Date and avoids a second timer on the event loop.
        """
        box = self._date_box if date_box is None else date_box

        def refresh() -> None:
            now = datetime.now(UTC)
            # Preformatted wire bytes; Writer concatenates without per-response format_datetime.
            box[0] = b"date: %s\r\n" % format_datetime(now, usegmt=True).encode("ascii")

        loop = asyncio.get_running_loop()
        setattr(loop, _DATE_TICK_SWEEPS_TIMEOUTS, True)

        async def tick() -> None:
            while True:
                await asyncio.sleep(1)
                refresh()
                self._sweep_connection_timeouts(loop, connections)

        refresh()  # first value before any connection can read it
        task = asyncio.create_task(tick())
        try:
            yield
        finally:
            setattr(loop, _DATE_TICK_SWEEPS_TIMEOUTS, False)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _drain_worker(self, connections: set[Connection], app: App) -> None:
        """Drain one worker loop (no asyncio.Server — the acceptor owns listen)."""
        for _ in range(_ACCEPT_REGISTER_YIELDS):
            if connections:
                break
            await asyncio.sleep(0)
        idle_closed = sum(
            1 for protocol in list(connections) if protocol.close_if_idle()
        )
        if idle_closed:
            await asyncio.sleep(0)
        await self._wait_for_managed_work_to_drain(connections, app.tasks)
        force_close_budget = min(
            max(self.config.graceful_shutdown_timeout, 0.0), _FORCE_CLOSE_CAP
        )
        loop = asyncio.get_running_loop()
        close_deadline = loop.time() + force_close_budget
        while connections and not self._urgent_drain and loop.time() < close_deadline:
            await self._force_close_open_transports(connections)
            await asyncio.sleep(0)
        if connections:
            await self._force_close_open_transports(connections)
        await self._cancel_pending_tasks(app.tasks)

    async def _run_worker(self, worker: _LoopWorker) -> None:
        """One worker thread's loop: Date tick, serve until shutdown, then drain."""
        loop = asyncio.get_running_loop()
        kind = require_configured_loop(
            self.config.event_loop,
            loop=loop,
            where=f"worker {worker.index}",
        )
        worker.loop = loop
        worker.loop_kind = kind
        worker.app.attach_loop(loop)
        worker.ready.set()
        async with self._date_tick(worker.date_box, worker.connections):
            try:
                await worker.app.shutdown
            finally:
                await self._drain_worker(worker.connections, worker.app)

    async def _serve_threaded(
        self,
        listen_sock: socket.socket | None,
        app: App,
        span: ProxySpan,
    ) -> None:
        """Accept on this loop; each worker thread runs `loop_runner()` (uvloop/asyncio)."""
        if listen_sock is None:
            raise StarioError(
                "threaded serve requires a bound listen socket",
                help_text="Set STARIO_HOST/STARIO_PORT or STARIO_UNIX_SOCKET.",
            )
        runner = self.loop_runner()
        workers = [
            _LoopWorker(
                index=index,
                app=app,
                server=self,
                loop_run=runner,
                body=self._run_worker,
            )
            for index in range(self.config.threads)
        ]
        for worker in workers:
            worker.start()
        join_timeout = (
            max(self.config.graceful_shutdown_timeout, 0.0) + _FORCE_CLOSE_CAP + 1.0
        )
        try:
            for worker in workers:
                started = await asyncio.to_thread(worker.ready.wait, 5.0)
                if not started:
                    raise StarioError(
                        f"worker {worker.index} failed to start",
                        help_text="Check worker thread logs; STARIO_LOOP must match a usable runner.",
                    )
                if worker.error is not None:
                    raise worker.error
            kinds = {worker.loop_kind for worker in workers}
            if kinds != {self.config.event_loop}:
                raise StarioError(
                    "worker event loops do not match STARIO_LOOP",
                    help_text=(
                        f"configured {self.config.event_loop}, workers running "
                        f"{', '.join(sorted(k for k in kinds if k is not None)) or 'nothing'}."
                    ),
                )
            span.attr("server.worker_event_loop", self.config.event_loop)
            span.attr("server.listening", True)
            span.end()
            try:
                await self._accept_loop(listen_sock, workers, app)
            except BaseException:
                self._open_shutdown_span(span, "runtime_failure")
                raise
            else:
                self._open_shutdown_span(span, "expected_stop")
            finally:
                app.signal_shutdown()
                with suppress(OSError):
                    listen_sock.close()
                for worker in workers:
                    await asyncio.to_thread(worker.join, join_timeout)
                await self._wait_for_managed_work_to_drain(set(), app.tasks)
                await self._cancel_pending_tasks(app.tasks)
                errors = [
                    worker.error for worker in workers if worker.error is not None
                ]
                if errors:
                    raise errors[0]
        except BaseException:
            app.signal_shutdown()
            with suppress(OSError):
                listen_sock.close()
            for worker in workers:
                await asyncio.to_thread(worker.join, min(join_timeout, 1.0))
            raise

    async def _accept_loop(
        self,
        listen_sock: socket.socket,
        workers: list[_LoopWorker],
        app: App,
    ) -> None:
        loop = asyncio.get_running_loop()

        async def accept_forever() -> None:
            while not app.shutting_down:
                try:
                    conn, _addr = await loop.sock_accept(listen_sock)
                except OSError, asyncio.CancelledError:
                    return
                if app.shutting_down:
                    with suppress(OSError):
                        conn.close()
                    return
                conn.setblocking(False)
                worker = min(workers, key=lambda item: len(item.connections))
                worker.submit(conn)

        task = asyncio.create_task(accept_forever(), name="stario.accept")
        try:
            await app.shutdown
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(OSError):
                listen_sock.close()

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
            "server.threads": self.config.threads,
            "server.tls": self.config.ssl is not None,
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
