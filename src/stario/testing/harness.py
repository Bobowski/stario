"""In-process response doubles for TestClient.

Requests are the production ``Request``; the writer and context are doubles
that only need to accept a handler call and collect a status, headers, and body.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from stario.http.context import (
    EMPTY_MATCH,
    Match,
    _Alive,  # pyright: ignore[reportPrivateUsage]
)
from stario.http.headers import Headers
from stario.telemetry.core import Span
from stario.testing.transport import GrowingSink

if TYPE_CHECKING:
    from stario.http.app import App
    from stario.http.request import Request


class TestWriter:
    """Collects one handler response: status, headers, body bytes."""

    __test__ = False

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

    def respond(
        self, body: bytes, content_type: bytes | str, status: int = 200
    ) -> None:
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

    def write_headers(self, status_code: int, *, body: bool = True) -> Self:
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

    async def drain(self) -> None:
        return None

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

    __test__ = False

    app: App
    req: Request
    span: Span
    _disconnect: asyncio.Future[None] = field(repr=False)
    state: dict[str, Any] = field(default_factory=dict[str, Any])
    match: Match = field(default=EMPTY_MATCH)

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
