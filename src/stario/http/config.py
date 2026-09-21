"""Process-level HTTP server configuration (listen, limits, compression, shutdown).

Body and header size defaults for `RequestPolicy` are defined in `request.py`
alongside `BodyReader`; this module re-exports them for env wiring.
"""

from collections.abc import Callable
from typing import Literal

from stario._env import (
    env_bool,
    env_float,
    env_int,
    env_octal_mode,
    env_optional_str,
    env_str,
)
from stario.exceptions import StarioError

from .compression import CompressionConfig, compression_config_from_env
from .request import (
    DEFAULT_BODY_TIMEOUT,
    DEFAULT_MAX_BODY_SIZE,
    DEFAULT_MAX_HEADER_BYTES,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT = 5.0
DEFAULT_BACKLOG = 2048
DEFAULT_UNIX_SOCKET_MODE = 0o660
DEFAULT_HEADER_TIMEOUT = 5.0
DEFAULT_KEEP_ALIVE_TIMEOUT = 5.0
DEFAULT_REUSE_ADDR = True
DEFAULT_MAX_PIPELINED_REQUESTS = 8
DEFAULT_EVENT_LOOP = "asyncio"

type EventLoopKind = Literal["asyncio", "uvloop"]


class RequestPolicy:
    """Header/body size caps, pipelining limit, and timeouts for a connection."""

    __slots__ = (
        "body_timeout",
        "header_timeout",
        "keep_alive_timeout",
        "max_body_bytes",
        "max_header_bytes",
        "max_pipelined_requests",
    )

    def __init__(
        self,
        *,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_body_bytes: int = DEFAULT_MAX_BODY_SIZE,
        header_timeout: float = DEFAULT_HEADER_TIMEOUT,
        body_timeout: float = DEFAULT_BODY_TIMEOUT,
        keep_alive_timeout: float = DEFAULT_KEEP_ALIVE_TIMEOUT,
        max_pipelined_requests: int = DEFAULT_MAX_PIPELINED_REQUESTS,
    ) -> None:
        if max_header_bytes < 256:
            raise StarioError(
                "max_header_bytes must be at least 256",
                help_text="Increase the limit or use the default Server settings.",
            )
        if max_body_bytes < 1:
            raise StarioError(
                "max_body_bytes must be at least 1",
                help_text="Use a positive byte limit for request bodies.",
            )
        for field, value in (
            ("header_timeout", header_timeout),
            ("body_timeout", body_timeout),
            ("keep_alive_timeout", keep_alive_timeout),
        ):
            if value <= 0:
                raise StarioError(
                    f"{field} must be greater than 0",
                    help_text="Use a positive timeout in seconds.",
                )
        if max_pipelined_requests < 1:
            raise StarioError(
                "max_pipelined_requests must be at least 1",
                help_text="Use 1 to disable pipelining beyond the in-flight request.",
            )

        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.header_timeout = header_timeout
        self.body_timeout = body_timeout
        self.keep_alive_timeout = keep_alive_timeout
        self.max_pipelined_requests = max_pipelined_requests


def _config_from_env[T](build: Callable[[], T]) -> T:
    try:
        return build()
    except ValueError as exc:
        raise StarioError(str(exc)) from exc


def request_policy_from_env() -> RequestPolicy:
    """Read `STARIO_REQUESTS_*` size caps, read timeouts, and keep-alive idle timeout."""
    return _config_from_env(
        lambda: RequestPolicy(
            max_header_bytes=env_int(
                "STARIO_REQUESTS_MAX_HEADER_BYTES", DEFAULT_MAX_HEADER_BYTES
            ),
            max_body_bytes=env_int(
                "STARIO_REQUESTS_MAX_BODY_BYTES", DEFAULT_MAX_BODY_SIZE
            ),
            header_timeout=env_float(
                "STARIO_REQUESTS_HEADER_TIMEOUT", DEFAULT_HEADER_TIMEOUT
            ),
            body_timeout=env_float(
                "STARIO_REQUESTS_BODY_TIMEOUT", DEFAULT_BODY_TIMEOUT
            ),
            keep_alive_timeout=env_float(
                "STARIO_REQUESTS_KEEP_ALIVE_TIMEOUT", DEFAULT_KEEP_ALIVE_TIMEOUT
            ),
            max_pipelined_requests=env_int(
                "STARIO_REQUESTS_MAX_PIPELINED_REQUESTS", DEFAULT_MAX_PIPELINED_REQUESTS
            ),
        )
    )


class ServerConfig:
    """Listen address, transport policy, request policy, compression, and shutdown.

    When `unix_socket` is set, `host`, `port`, and `reuse_addr` are ignored; only
    `backlog` and `unix_socket_mode` apply to the Unix listener.

    Nested `requests` and `compression` objects are stored by reference; treat
    as immutable after construction.

    `graceful_shutdown_timeout` is the phase-1 drain window (handlers finish,
    idle connections close). Shutdown may continue up to one additional second
    in a force-close loop before orphaned tasks are cancelled (see
    `Server._drain_listener`).
    """

    __slots__ = (
        "backlog",
        "compression",
        "event_loop",
        "graceful_shutdown_timeout",
        "host",
        "port",
        "requests",
        "reuse_addr",
        "unix_socket",
        "unix_socket_mode",
    )

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        unix_socket: str | None = None,
        unix_socket_mode: int = DEFAULT_UNIX_SOCKET_MODE,
        requests: RequestPolicy | None = None,
        compression: CompressionConfig | None = None,
        graceful_shutdown_timeout: float = DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
        backlog: int = DEFAULT_BACKLOG,
        reuse_addr: bool = DEFAULT_REUSE_ADDR,
        event_loop: EventLoopKind = DEFAULT_EVENT_LOOP,
    ) -> None:
        if not 1 <= port <= 65535:
            raise StarioError(
                "port must be between 1 and 65535",
                help_text="Use a valid TCP port number.",
            )
        if graceful_shutdown_timeout < 0:
            raise StarioError(
                "graceful_shutdown_timeout must be greater than or equal to 0",
                help_text="Use zero for immediate shutdown or a positive drain window in seconds.",
            )
        if backlog < 1:
            raise StarioError(
                "backlog must be at least 1",
                help_text="Use a positive socket listen backlog.",
            )
        if not 0 <= unix_socket_mode <= 0o7777:
            raise StarioError(
                "unix_socket_mode must be between 0 and 7777 (octal)",
                help_text="Use a standard Unix file mode such as 660.",
            )
        if unix_socket is not None and not unix_socket.strip():
            raise StarioError(
                "unix_socket must be a non-empty path",
                help_text="Omit unix_socket for TCP listen or set STARIO_UNIX_SOCKET to a socket path.",
            )
        if unix_socket is None and not host.strip():
            raise StarioError(
                "host must be non-empty for TCP listen",
                help_text="Set STARIO_HOST or pass a non-empty host to ServerConfig.",
            )
        if event_loop not in ("asyncio", "uvloop"):
            raise StarioError(
                "event_loop must be 'asyncio' or 'uvloop'",
                help_text="Set STARIO_LOOP or pass event_loop to ServerConfig.",
            )

        self.host = host
        self.port = port
        self.unix_socket = unix_socket
        self.unix_socket_mode = unix_socket_mode
        self.requests = requests if requests is not None else RequestPolicy()
        self.compression = (
            compression if compression is not None else CompressionConfig()
        )
        self.graceful_shutdown_timeout = graceful_shutdown_timeout
        self.backlog = backlog
        self.reuse_addr = reuse_addr
        self.event_loop: EventLoopKind = event_loop


def _event_loop_from_env() -> EventLoopKind:
    loop = env_str("STARIO_LOOP", DEFAULT_EVENT_LOOP).lower()
    if loop not in ("asyncio", "uvloop"):
        raise StarioError(
            "STARIO_LOOP must be 'asyncio' or 'uvloop'",
            help_text="Set STARIO_LOOP to asyncio or uvloop.",
        )
    return loop


def server_config_from_env() -> ServerConfig:
    """Read `STARIO_*` listen, limit, compression, and shutdown settings."""
    return _config_from_env(
        lambda: ServerConfig(
            host=env_str("STARIO_HOST", DEFAULT_HOST),
            port=env_int("STARIO_PORT", DEFAULT_PORT),
            unix_socket=env_optional_str("STARIO_UNIX_SOCKET"),
            unix_socket_mode=env_octal_mode(
                "STARIO_UNIX_SOCKET_MODE", DEFAULT_UNIX_SOCKET_MODE
            ),
            compression=compression_config_from_env(),
            requests=request_policy_from_env(),
            graceful_shutdown_timeout=env_float(
                "STARIO_GRACEFUL_SHUTDOWN_TIMEOUT", DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT
            ),
            backlog=env_int("STARIO_BACKLOG", DEFAULT_BACKLOG),
            reuse_addr=env_bool("STARIO_REUSE_ADDR", DEFAULT_REUSE_ADDR),
            event_loop=_event_loop_from_env(),
        )
    )
