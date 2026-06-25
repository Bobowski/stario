"""Buffered and streaming HTTP test responses."""

import asyncio
import json as json_module
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Self
from uuid import UUID

from stario.http.headers import Headers
from stario.testing.models import ClientRequest
from stario.testing.transport import (
    GrowingSink,
    parse_chunk_size_line,
    wait_sink,
)


@dataclass(slots=True)
class TestResponse:
    """Buffered HTTP result from `TestClient.get`, `TestClient.post`, etc.

    The body is fully buffered; `Content-Encoding` is decoded when present.
    Pair `span_id` with `TestClient.tracer` for assertions.
    """

    __test__ = False

    status_code: int
    url: str
    headers: Headers
    content: bytes
    request: ClientRequest
    span_id: UUID
    _disconnect_future: asyncio.Future[None] = field(repr=False)
    history: list[TestResponse] = field(default_factory=lambda: [])
    cookies: dict[str, str] = field(default_factory=lambda: {})

    @property
    def disconnect(self) -> asyncio.Future[None]:
        return self._disconnect_future

    @property
    def text(self) -> str:
        charset = "utf-8"
        if ct := self.headers.get("content-type"):
            for part in ct.split(";")[1:]:
                key, _, value = part.strip().partition("=")
                if key.lower() == "charset" and value:
                    charset = value.strip()
                    break
        return self.content.decode(charset, errors="replace")

    def json(self) -> Any:
        return json_module.loads(self.content)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def reason_phrase(self) -> str:
        try:
            return HTTPStatus(self.status_code).phrase
        except ValueError:
            return ""

    @property
    def is_redirect(self) -> bool:
        return (
            self.status_code in {301, 302, 303, 307, 308}
            and self.headers.get("location") is not None
        )

    def raise_for_status(self) -> Self:
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} {self.reason_phrase}: {self.text}")
        return self


@dataclass(slots=True)
class TestStreamResponse:
    """Streaming result from `TestClient.stream` once response headers exist.

    Use `body`, `iter_bytes`, or `iter_events` to read the entity body.
    Leaving the `stream` context disconnects this exchange and awaits the app task.
    """

    __test__ = False

    status_code: int
    url: str
    headers: Headers
    request: ClientRequest
    span_id: UUID
    cookies: dict[str, str]
    sink: GrowingSink
    _body_start: int
    _chunked: bool
    _content_length: int | None
    _disconnect_future: asyncio.Future[None]
    _app_task: asyncio.Task[None]
    _deadline: float | None = field(repr=False, default=None)

    @property
    def disconnect(self) -> asyncio.Future[None]:
        return self._disconnect_future

    async def body(self) -> bytes:
        """Concatenate all body chunks after transfer decoding (same as iterating `iter_bytes`)."""

        parts: list[bytes] = []
        async for chunk in self.iter_bytes():
            parts.append(chunk)
        return b"".join(parts)

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """Yield decoded body chunks as they arrive (chunked / fixed-length / until close).

        Only `identity` `Content-Encoding` is supported; request
        `Accept-Encoding: identity` or use buffered methods for compression.
        """

        ce = (self.headers.get("content-encoding") or "").strip().lower()
        if ce and ce != "identity":
            raise RuntimeError(
                "TestClient.iter_bytes does not support Content-Encoding="
                f"{ce!r}; use accept-encoding that yields identity or use buffered client.get()."
            )

        sink = self.sink
        pos = self._body_start
        deadline = self._deadline

        if self.status_code in {204, 304} or self.request.method == "HEAD":
            return

        if self._chunked:
            while True:
                parsed = parse_chunk_size_line(sink.buf, pos)
                while parsed is None:
                    if sink.app_done:
                        raise RuntimeError("Incomplete chunked encoding in stream.")
                    seen = sink.gen
                    await wait_sink(sink, seen, deadline=deadline)
                    parsed = parse_chunk_size_line(sink.buf, pos)
                chunk_size, pos = parsed
                if chunk_size == 0:
                    return
                end_data = pos + chunk_size
                while len(sink.buf) < end_data + 2:
                    if sink.app_done and len(sink.buf) < end_data:
                        raise RuntimeError("Truncated chunked body in stream.")
                    seen = sink.gen
                    await wait_sink(sink, seen, deadline=deadline)
                payload = bytes(sink.buf[pos:end_data])
                pos = end_data + 2
                if payload:
                    yield payload

        elif self._content_length is not None:
            remain = self._content_length
            while remain > 0:
                avail = len(sink.buf) - pos
                while avail <= 0:
                    if sink.app_done:
                        raise RuntimeError("Truncated fixed-length body in stream.")
                    seen = sink.gen
                    await wait_sink(sink, seen, deadline=deadline)
                    avail = len(sink.buf) - pos
                take = min(avail, remain)
                yield bytes(sink.buf[pos : pos + take])
                pos += take
                remain -= take

        else:
            while True:
                avail = len(sink.buf) - pos
                while avail <= 0 and not sink.app_done:
                    seen = sink.gen
                    await wait_sink(sink, seen, deadline=deadline)
                    avail = len(sink.buf) - pos
                if avail > 0:
                    yield bytes(sink.buf[pos:])
                    pos = len(sink.buf)
                if sink.app_done:
                    return

    async def iter_events(self) -> AsyncIterator[dict[str, str]]:
        """Parse `text/event-stream` into dicts (keys such as `event`, `id`, `data`)."""

        buffer = b""
        async for chunk in self.iter_bytes():
            buffer += chunk
            while True:
                idx = buffer.find(b"\r\n\r\n")
                sep_len = 4
                if idx < 0:
                    idx = buffer.find(b"\n\n")
                    sep_len = 2
                if idx < 0:
                    break
                raw = buffer[:idx]
                buffer = buffer[idx + sep_len :]
                text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
                data_lines: list[str] = []
                ev: dict[str, str] = {}
                for line in text.split("\n"):
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith(" "):
                        if data_lines:
                            data_lines[-1] += line[1:]
                        continue
                    if ":" in line:
                        fld, _, rest = line.partition(":")
                        rest = rest.lstrip(" ")
                    else:
                        fld, rest = line, ""
                    fld = fld.strip()
                    if fld == "data":
                        data_lines.append(rest)
                    elif fld in ("event", "id"):
                        ev[fld] = rest
                if data_lines:
                    ev["data"] = "\n".join(data_lines)
                yield ev
