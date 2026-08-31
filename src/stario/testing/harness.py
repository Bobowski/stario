"""In-process request/response doubles for TestClient.

These are not the production Writer / Request / Context. They only need to
accept a handler call and collect a status, headers, and body.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from stario.exceptions import RequestBodyError
from stario.http.context import EMPTY_ROUTE_MATCH, RouteMatch, _Alive
from stario.http.headers import Headers
from stario.http.host import host_without_port
from stario.http.query import ParsedQuery
from stario.http.request import ParsedCookies
from stario.testing.transport import GrowingSink
from stario.telemetry.core import Span

if TYPE_CHECKING:
    from stario.http.app import App
    from stario.http.request import Request


class TestRequest:
    """Simple request the test client passes into ``app(c, w)``."""

    __slots__ = (
        "_body",
        "_consumed_stream",
        "_cookies",
        "_host",
        "_query",
        "headers",
        "keep_alive",
        "method",
        "path",
        "protocol_version",
        "query_bytes",
    )

    def __init__(
        self,
        *,
        method: str = "GET",
        path: str = "/",
        query_bytes: bytes = b"",
        headers: Headers | None = None,
        body: bytes = b"",
        protocol_version: str = "1.1",
        keep_alive: bool = True,
    ) -> None:
        self.method = method
        self.path = path
        self.query_bytes = query_bytes if query_bytes else b""
        self.headers = headers if headers is not None else Headers()
        self.protocol_version = protocol_version
        self.keep_alive = keep_alive
        self._body = body
        self._query: ParsedQuery | None = None
        self._cookies: ParsedCookies | None = None
        self._host: str | None = None
        self._consumed_stream = False

    @property
    def host(self) -> str:
        if self._host is None:
            self._host = host_without_port(self.headers.get("host") or "")
        return self._host

    @property
    def query(self) -> ParsedQuery:
        if self._query is None:
            self._query = ParsedQuery(self.query_bytes)
        return self._query

    @property
    def cookies(self) -> ParsedCookies:
        if self._cookies is None:
            self._cookies = ParsedCookies(self.headers.getlist("cookie"))
        return self._cookies

    async def body(self, max_size: int | None = None) -> bytes:
        if max_size is not None and max_size < 0:
            raise ValueError("max_size must be non-negative.")
        if self._consumed_stream:
            raise RuntimeError("Request body was already streamed.")
        if max_size is not None and len(self._body) > max_size:
            raise RequestBodyError(413, "Request body too large")
        return self._body

    async def stream(self, max_chunk: int | None = None) -> AsyncIterator[bytes]:
        if self._consumed_stream:
            raise RuntimeError("Request body is already streaming.")
        self._consumed_stream = True
        if self._body:
            yield self._body


class TestWriter:
    """Collects one handler response: status, headers, body bytes."""

    def __init__(self, disconnect: asyncio.Future[None] | None = None) -> None:
        self.headers = Headers()
        self._status_code: int | None = None
        self._completed = False
        self._disconnect = disconnect
        self._body = GrowingSink()
        self._headers_event = asyncio.Event()

    @property
    def status_code(self) -> int | None:
        return self._status_code

    @property
    def started(self) -> bool:
        return self._status_code is not None

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def closing(self) -> bool:
        return self._completed or (
            self._disconnect is not None and self._disconnect.done()
        )

    @property
    def body(self) -> bytes:
        return bytes(self._body.buf)

    @property
    def sink(self) -> GrowingSink:
        return self._body

    @property
    def headers_sent(self) -> asyncio.Event:
        return self._headers_event

    def _mark_headers(self, status: int) -> None:
        self._status_code = status
        if not self._headers_event.is_set():
            self._headers_event.set()

    def respond(self, body: bytes, content_type: bytes | str, status: int = 200) -> None:
        if self._completed:
            return
        if self._status_code is not None:
            raise RuntimeError(
                "Response already started (headers sent). "
                "Set headers via w.headers.set() before the first write or one-shot respond()."
            )
        ctype = (
            content_type.decode("latin-1")
            if isinstance(content_type, bytes)
            else content_type
        )
        self.headers.set("content-type", ctype)
        payload = body if body else b""
        if 100 <= status < 200 or status in {204, 304}:
            payload = b""
        self.headers.set("content-length", str(len(payload)))
        self._mark_headers(status)
        if payload:
            self._body.extend(payload)
        self._completed = True
        self._body.mark_app_done()

    def write_headers(self, status_code: int) -> Self:
        if self._status_code is not None:
            raise RuntimeError(
                "Response already started (headers sent). "
                "Set headers via w.headers.set() before the first write or one-shot respond()."
            )
        self._mark_headers(status_code)
        return self

    def write(self, data: bytes) -> Self:
        if self._completed:
            raise RuntimeError(
                "Cannot write after response is completed. "
                "This happens after calling w.end() or a response helper has "
                "already finalized the writer."
            )
        if self._status_code is None:
            self.write_headers(200)
        if self._status_code is not None and (
            self._status_code in {204, 304} or 100 <= self._status_code < 200
        ):
            raise RuntimeError(
                f"Cannot write a body for HTTP {self._status_code} responses."
            )
        if data:
            self._body.extend(data)
        return self

    def end(self, data: bytes | None = None) -> None:
        if self._completed:
            return
        if self._status_code is None:
            self.write_headers(200 if data else 204)
        if data:
            self.write(data)
        self._completed = True
        self._body.mark_app_done()

    def abort(self) -> None:
        if self._completed:
            return
        self._completed = True
        self.headers.set("connection", "close")
        self._body.mark_app_done()


@dataclass(slots=True)
class TestContext:
    """Handler context for in-process TestClient exchanges."""

    app: App
    req: Request
    span: Span
    _disconnect: asyncio.Future[None] = field(repr=False)
    state: dict[str, Any] = field(default_factory=dict)
    route: RouteMatch = field(default=EMPTY_ROUTE_MATCH)

    @property
    def disconnect(self) -> asyncio.Future[None]:
        return self._disconnect

    @property
    def disconnected(self) -> bool:
        return self._disconnect.done()

    @property
    def shutting_down(self) -> bool:
        return self.app.shutting_down

    @property
    def closing(self) -> bool:
        return self.disconnected or self.shutting_down

    def alive(
        self,
        source: Any = None,
    ) -> _Alive[Any]:
        return _Alive(self, source)
