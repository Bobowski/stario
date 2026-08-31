"""Request protocol and small host/limit helpers.

Runtime ``Request`` and ``ParsedCookies`` are Cython types from
``stario_cython.exchange``. ``host_without_port`` and the default size/timeout
constants stay here because config and the protocol import them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from stario.http.host import host_without_port

# =============================================================================
# Security limits (used by ServerConfig / RequestPolicy)
# =============================================================================
DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
DEFAULT_MAX_HEADER_BYTES = 64 * 1024  # 64 KiB
DEFAULT_BODY_TIMEOUT = 30.0  # seconds


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from stario.http.headers import Headers
    from stario.http.query import ParsedQuery

    class ParsedCookies(Protocol, Mapping[str, str]):
        def get(self, key: str, default: str | None = None) -> str | None: ...
        def as_dict(self) -> dict[str, str]: ...
        def items(self) -> list[tuple[str, str]]: ...
        def keys(self) -> list[str]: ...
        def values(self) -> list[str]: ...

    class Request(Protocol):
        method: str
        path: str
        headers: Headers
        protocol_version: str
        keep_alive: bool

        @property
        def query_bytes(self) -> bytes: ...

        @property
        def host(self) -> str: ...

        @property
        def query(self) -> ParsedQuery: ...

        @property
        def cookies(self) -> ParsedCookies: ...

        async def body(self, max_size: int | None = None) -> bytes: ...

        def stream(self, max_chunk: int | None = None) -> AsyncIterator[bytes]: ...

else:
    from stario_cython.exchange import ParsedCookies, Request

__all__ = [
    "DEFAULT_BODY_TIMEOUT",
    "DEFAULT_MAX_BODY_SIZE",
    "DEFAULT_MAX_HEADER_BYTES",
    "ParsedCookies",
    "Request",
    "host_without_port",
]
