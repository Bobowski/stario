"""Buffered and streaming HTTP test responses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Self
from uuid import UUID

from stario.http.headers import Headers
from stario.json import loads as json_loads
from stario.testing.models import ClientRequest
from stario.testing.wire import Exchange


def _charset(headers: Headers) -> str:
    if ct := headers.get("content-type"):
        for part in ct.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                return value.strip()
    return "utf-8"


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
    history: list[TestResponse] = field(default_factory=lambda: [])
    cookies: dict[str, str] = field(default_factory=lambda: {})

    @property
    def text(self) -> str:
        return self.content.decode(_charset(self.headers), errors="replace")

    def json(self) -> Any:
        return json_loads(self.content)

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

    Use `body`, `iter_bytes`, or `iter_events` to read the entity body as the
    handler writes it. Leaving the `stream` context hangs up the connection
    (the handler sees `c.disconnected`) and waits for the handler to finish.
    """

    __test__ = False

    status_code: int
    url: str
    headers: Headers
    request: ClientRequest
    span_id: UUID
    cookies: dict[str, str]
    _exchange: Exchange = field(repr=False)
    _deadline: float | None = field(repr=False, default=None)

    @property
    def disconnected(self) -> bool:
        """True once the test (or the server) closed this connection."""
        return self._exchange.transport.is_closing()

    def disconnect(self) -> None:
        """Hang up now instead of at the end of the `stream` block."""
        self._exchange.disconnect()

    async def body(self) -> bytes:
        """Concatenate all body chunks after they arrive."""
        return b"".join([chunk async for chunk in self.iter_bytes()])

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """Yield body chunks as the handler writes them (framing removed)."""
        ce = (self.headers.get("content-encoding") or "").strip().lower()
        if ce and ce != "identity":
            raise RuntimeError(
                "TestClient.iter_bytes does not support Content-Encoding="
                f"{ce!r}; send Accept-Encoding: identity or use the buffered client.get()."
            )
        reader = self._exchange.reader
        while (chunk := await reader.next_chunk(self._deadline)) is not None:
            yield chunk

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
                yield _parse_event(raw)


def _parse_event(raw: bytes) -> dict[str, str]:
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
    return ev
