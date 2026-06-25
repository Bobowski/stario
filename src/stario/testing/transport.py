"""In-memory transport and streaming sink helpers."""

import asyncio
from collections.abc import Callable

from stario.http.headers import Headers


class MemoryTransport(asyncio.Transport):
    """In-memory transport for TestClient dispatch."""

    __slots__ = ("_closing", "_write")

    def __init__(self, write: Callable[[bytes], None] | None = None) -> None:
        super().__init__()
        self._write = write
        self._closing = False

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._closing or self._write is None:
            return
        if type(data) is bytes:
            self._write(data)
        else:
            self._write(bytes(data))

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


class GrowingSink:
    __slots__ = ("_app_done", "_app_exc", "_event", "_gen", "buf")

    def __init__(self) -> None:
        self.buf = bytearray()
        self._gen = 0
        self._event = asyncio.Event()
        self._app_done = False
        self._app_exc: BaseException | None = None

    @property
    def app_done(self) -> bool:
        return self._app_done

    def extend(self, data: bytes) -> None:
        if data:
            self.buf.extend(data)
            self._gen += 1
            self._event.set()

    def mark_app_done(self, exc: BaseException | None = None) -> None:
        self._app_done = True
        self._app_exc = exc
        self._gen += 1
        self._event.set()

    @property
    def gen(self) -> int:
        return self._gen

    async def wait(self, seen_gen: int) -> None:
        while self._gen == seen_gen and not self._app_done:
            await self._event.wait()
            self._event.clear()
        if self._app_exc is not None:
            raise self._app_exc


async def wait_sink(
    sink: GrowingSink,
    seen_gen: int,
    *,
    deadline: float | None = None,
) -> None:
    """Wait for sink progress; ``deadline`` is an absolute ``loop.time()`` limit."""

    if deadline is None:
        await sink.wait(seen_gen)
        return
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError()
    await asyncio.wait_for(sink.wait(seen_gen), timeout=remaining)


def try_parse_http_head(buf: bytes | bytearray) -> tuple[int, Headers, int] | None:
    head, sep, _ = buf.partition(b"\r\n\r\n")
    if not sep:
        return None
    header_end = len(head) + 4
    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ", 2)
    if len(status_parts) < 2:
        raise RuntimeError("Malformed test response: invalid status line.")
    status_code = int(status_parts[1])
    headers = Headers()
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers.unsafe_add(bytes(name.lower()), bytes(value.lstrip()))
    return status_code, headers, header_end


def parse_chunk_size_line(buf: bytes | bytearray, pos: int) -> tuple[int, int] | None:
    """Return ``(chunk_size, payload_start)`` or ``None`` when the size line is incomplete."""

    nl = buf.find(b"\r\n", pos)
    if nl < 0:
        return None
    chunk_size = int(buf[pos:nl].split(b";", 1)[0], 16)
    return chunk_size, nl + 2


def decode_chunked(body: bytes | bytearray, *, start: int = 0) -> bytes:
    """Decode an HTTP/1.1 chunked transfer body from ``start`` to the end."""

    pos = start
    decoded = bytearray()
    while pos < len(body):
        parsed = parse_chunk_size_line(body, pos)
        if parsed is None:
            if decoded:
                return bytes(decoded)
            raise RuntimeError("Incomplete chunked encoding.")
        chunk_size, pos = parsed
        if chunk_size == 0:
            break
        end_data = pos + chunk_size
        if len(body) < end_data:
            raise RuntimeError("Truncated chunked body.")
        decoded.extend(body[pos:end_data])
        pos = end_data
        if pos + 2 <= len(body):
            pos += 2
        else:
            break
    return bytes(decoded)
