# pyright: reportMissingImports=false

import ujson

from apps.common import (
    PLAINTEXT_BODY,
    REQUEST_HEADER,
    TEXT_CONTENT_TYPE_STR,
    as_str,
    bytes_line,
    json_echo_line,
    query_param,
    request_line,
    yield_once,
)

TEXT_HEADERS = [("content-type", TEXT_CONTENT_TYPE_STR)]


async def _read_buffer(proto) -> bytes:
    return await proto()


async def _read_stream(proto) -> int:
    total = 0
    async for chunk in proto:
        total += len(chunk)
    return total


def _text(proto, line: str) -> None:
    proto.response_bytes(200, TEXT_HEADERS, line.encode("ascii"))


async def app(scope, proto):
    if scope.proto != "http":
        return

    path, method = scope.path, scope.method

    if method == "GET" and path == "/plaintext":
        proto.response_bytes(200, TEXT_HEADERS, PLAINTEXT_BODY)
    elif method == "GET" and path.startswith("/user/"):
        user_id = path.rsplit("/", 1)[-1]
        _text(
            proto,
            request_line(
                user_id,
                query_param(scope.query_string, "q"),
                as_str(scope.headers.get(REQUEST_HEADER)),
            ),
        )
    elif method == "POST" and path == "/echo":
        raw = await _read_buffer(proto)
        await yield_once()
        _text(proto, json_echo_line(ujson.loads(raw) if raw else {}))
    elif method == "POST" and path in {"/ingest/64k", "/ingest/2m"}:
        raw = await _read_buffer(proto)
        await yield_once()
        _text(proto, bytes_line(len(raw)))
    elif method == "POST" and path == "/ingest/stream/2m":
        total = await _read_stream(proto)
        await yield_once()
        _text(proto, bytes_line(total))
    elif method == "POST" and path == "/upload":
        raw = await _read_buffer(proto)
        await yield_once()
        _text(proto, bytes_line(len(raw)))
    else:
        proto.response_str(404, [], "Not Found")
