"""Shared Cython protocol test helpers."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

from stario_cython.protocol import HttpProtocol

from stario.http.compression import CompressionConfig
from stario.telemetry.noop import NoOpTracer


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def running_server(app, *, date: bytes, compression=None):
    loop = asyncio.get_running_loop()
    connections: set[HttpProtocol] = set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [date],
            CompressionConfig() if compression is None else compression,
            connections,
        ),
        "127.0.0.1",
        free_port(),
    )
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def read_response(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(2):
        header = await reader.readuntil(b"\r\n\r\n")
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
                return header + (await reader.readexactly(length) if length else b"")
    return header


async def read_chunk(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(2):
        size = int((await reader.readuntil(b"\r\n"))[:-2], 16)
        if size == 0:
            assert await reader.readexactly(2) == b"\r\n"
            return b""
        data = await reader.readexactly(size)
        assert await reader.readexactly(2) == b"\r\n"
        return data
