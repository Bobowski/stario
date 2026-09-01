"""Minimal HTTP/2 client frames for protocol tests (no extra HPACK package)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

TYPE_DATA = 0x0
TYPE_HEADERS = 0x1
TYPE_RST_STREAM = 0x3
TYPE_SETTINGS = 0x4
TYPE_GOAWAY = 0x7
TYPE_WINDOW_UPDATE = 0x8

FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4

NGHTTP2_PROTOCOL_ERROR = 1

SETTINGS_MAX_HEADER_LIST_SIZE = 0x6
SETTINGS_NO_RFC7540_PRIORITIES = 0x9

IDX_AUTHORITY = 1
IDX_METHOD_GET = 2
IDX_METHOD_POST = 3
IDX_PATH_SLASH = 4
IDX_SCHEME_HTTP = 6
IDX_CONTENT_LENGTH = 28
IDX_HOST = 38


@dataclass(frozen=True, slots=True)
class H2Frame:
    type: int
    flags: int
    stream_id: int
    payload: bytes


def pack_frame(ftype: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    if len(payload) >= 1 << 24:
        raise ValueError("frame too large")
    return (
        len(payload).to_bytes(3, "big")
        + bytes((ftype, flags))
        + (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
        + payload
    )


def hpack_int(value: int, prefix_bits: int, prefix_tag: int) -> bytes:
    max_prefix = (1 << prefix_bits) - 1
    first = prefix_tag | min(value, max_prefix)
    if value < max_prefix:
        return bytes((first,))
    out = bytearray((first,))
    value -= max_prefix
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def hpack_string(data: bytes) -> bytes:
    return hpack_int(len(data), 7, 0x00) + data


def indexed(index: int) -> bytes:
    return hpack_int(index, 7, 0x80)


def literal_name(index: int, value: bytes) -> bytes:
    """Literal header field without indexing — indexed name."""
    return hpack_int(index, 4, 0x00) + hpack_string(value)


def literal_new(name: bytes, value: bytes) -> bytes:
    return b"\x00" + hpack_string(name) + hpack_string(value)


def encode_request(
    *,
    method: str = "GET",
    path: str = "/",
    authority: str = "127.0.0.1",
    extra: list[tuple[bytes, bytes]] | None = None,
) -> bytes:
    parts: list[bytes] = []
    if method == "GET":
        parts.append(indexed(IDX_METHOD_GET))
    elif method == "POST":
        parts.append(indexed(IDX_METHOD_POST))
    else:
        parts.append(literal_name(IDX_METHOD_GET, method.encode("ascii")))
    parts.append(indexed(IDX_SCHEME_HTTP))
    if path == "/":
        parts.append(indexed(IDX_PATH_SLASH))
    else:
        parts.append(literal_name(IDX_PATH_SLASH, path.encode("ascii")))
    parts.append(literal_name(IDX_AUTHORITY, authority.encode("ascii")))
    for name, value in extra or []:
        if name == b"host":
            parts.append(literal_name(IDX_HOST, value))
        elif name == b"content-length":
            parts.append(literal_name(IDX_CONTENT_LENGTH, value))
        else:
            parts.append(literal_new(name, value))
    return b"".join(parts)


def headers_duplicate_path() -> bytes:
    return (
        indexed(IDX_METHOD_GET)
        + indexed(IDX_SCHEME_HTTP)
        + indexed(IDX_PATH_SLASH)
        + literal_name(IDX_PATH_SLASH, b"/other")
        + literal_name(IDX_AUTHORITY, b"127.0.0.1")
    )


def headers_duplicate_method() -> bytes:
    return (
        indexed(IDX_METHOD_GET)
        + indexed(IDX_METHOD_POST)
        + indexed(IDX_SCHEME_HTTP)
        + indexed(IDX_PATH_SLASH)
        + literal_name(IDX_AUTHORITY, b"127.0.0.1")
    )


def parse_frames(buf: bytes) -> tuple[list[H2Frame], bytes]:
    frames: list[H2Frame] = []
    i = 0
    while i + 9 <= len(buf):
        length = int.from_bytes(buf[i : i + 3], "big")
        end = i + 9 + length
        if end > len(buf):
            break
        frames.append(
            H2Frame(
                buf[i + 3],
                buf[i + 4],
                int.from_bytes(buf[i + 5 : i + 9], "big") & 0x7FFFFFFF,
                buf[i + 9 : end],
            )
        )
        i = end
    return frames, buf[i:]


async def h2_handshake(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(H2_PREFACE + pack_frame(TYPE_SETTINGS, 0, 0))
    await writer.drain()
    buf = b""
    async with asyncio.timeout(2):
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                writer.close()
                raise ConnectionError("closed during HTTP/2 handshake")
            buf += chunk
            frames, rest = parse_frames(buf)
            buf = rest
            if any(
                frame.type == TYPE_SETTINGS and not (frame.flags & FLAG_ACK)
                for frame in frames
            ):
                writer.write(pack_frame(TYPE_SETTINGS, FLAG_ACK, 0))
                await writer.drain()
                return reader, writer, buf


def collected_settings(frames: list[H2Frame]) -> dict[int, int]:
    out: dict[int, int] = {}
    for frame in frames:
        if frame.type != TYPE_SETTINGS or (frame.flags & FLAG_ACK):
            continue
        payload = frame.payload
        for i in range(0, len(payload) - 5, 6):
            sid = int.from_bytes(payload[i : i + 2], "big")
            out[sid] = int.from_bytes(payload[i + 2 : i + 6], "big")
    return out


def stream_data(frames: list[H2Frame], stream_id: int) -> bytes:
    return b"".join(
        frame.payload
        for frame in frames
        if frame.type == TYPE_DATA and frame.stream_id == stream_id
    )


def stream_headers_blob(frames: list[H2Frame], stream_id: int) -> bytes:
    return b"".join(
        frame.payload
        for frame in frames
        if frame.type == TYPE_HEADERS and frame.stream_id == stream_id
    )


def has_rst(frames: list[H2Frame], stream_id: int) -> bool:
    return any(
        frame.type == TYPE_RST_STREAM and frame.stream_id == stream_id
        for frame in frames
    )


def rst_code(frames: list[H2Frame], stream_id: int) -> int | None:
    for frame in frames:
        if frame.type == TYPE_RST_STREAM and frame.stream_id == stream_id:
            if len(frame.payload) >= 4:
                return int.from_bytes(frame.payload[:4], "big")
            return 0
    return None


def has_goaway(frames: list[H2Frame]) -> bool:
    return any(frame.type == TYPE_GOAWAY for frame in frames)


def stream_ended(frames: list[H2Frame], stream_id: int) -> bool:
    if has_rst(frames, stream_id) or has_goaway(frames):
        return True
    return any(
        frame.stream_id == stream_id
        and frame.type in (TYPE_HEADERS, TYPE_DATA)
        and (frame.flags & FLAG_END_STREAM)
        for frame in frames
    )


async def read_stream(
    reader: asyncio.StreamReader,
    buf: bytes,
    stream_id: int,
    *,
    timeout: float = 2.0,
) -> tuple[list[H2Frame], bytes]:
    collected: list[H2Frame] = []
    async with asyncio.timeout(timeout):
        while not stream_ended(collected, stream_id):
            parsed, buf = parse_frames(buf)
            collected.extend(parsed)
            if stream_ended(collected, stream_id):
                break
            chunk = await reader.read(65536)
            if not chunk:
                break
            buf += chunk
    return collected, buf
