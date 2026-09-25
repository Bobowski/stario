import asyncio
from typing import Any

# Runtime base is asyncio.BufferedProtocol; data_received is implemented too,
# so the class also satisfies asyncio.Protocol.
class HttpProtocol(asyncio.Protocol):
    loop: asyncio.AbstractEventLoop
    transport: asyncio.Transport | None
    disconnect: asyncio.Future[None] | None
    closed: bool
    parse_mode: int
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        app: Any,
        tracer: Any,
        date_box: list[bytes],
        compression: Any,
        connections: set[Any],
        max_header_bytes: int = ...,
        max_body_bytes: int = ...,
        header_timeout: float = ...,
        keep_alive_timeout: float = ...,
        body_timeout: float = ...,
        max_pipelined_requests: int = ...,
        timeout_cleanup: str | int | None = None,
        timeout_sweep_interval: float | None = None,
    ) -> None: ...
    def data_received(self, data: bytes | bytearray | memoryview) -> None: ...
    def get_buffer(self, sizehint: int) -> memoryview: ...
    def buffer_updated(self, nbytes: int) -> None: ...
    def check_timeouts(self, now: float) -> None: ...
    def close_if_idle(self) -> bool: ...
