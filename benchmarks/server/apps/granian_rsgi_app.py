# pyright: reportMissingImports=false

import ujson

from apps.common import validate_fields

HELLO = "Hello, World!"
JSON_HEADERS = [("content-type", "application/json")]


def _json_bytes(value: object) -> bytes:
    return ujson.dumps(value).encode()


async def _read_buffer(proto) -> bytes:
    return await proto()


async def _read_stream(proto) -> int:
    total = 0
    async for chunk in proto:
        total += len(chunk)
    return total


async def app(scope, proto):
    if scope.proto != "http":
        return

    path, method = scope.path, scope.method

    if method == "GET" and path == "/plaintext":
        proto.response_str(
            200,
            [("content-type", "text/plain; charset=utf-8")],
            HELLO,
        )
    elif method == "GET" and path == "/json":
        proto.response_bytes(
            200,
            JSON_HEADERS,
            _json_bytes({"message": HELLO}),
        )
    elif method == "GET" and path.startswith("/user/"):
        user_id = path.rsplit("/", 1)[-1]
        proto.response_bytes(
            200,
            JSON_HEADERS,
            _json_bytes({"id": user_id, "name": f"User {user_id}"}),
        )
    elif method == "POST" and path == "/validate":
        raw = await _read_buffer(proto)
        payload, status = validate_fields(ujson.loads(raw))
        proto.response_bytes(status, JSON_HEADERS, _json_bytes(payload))
    elif method == "POST" and path == "/form":
        await _read_buffer(proto)
        proto.response_empty(204, [])
    elif method == "POST" and path == "/echo/json":
        raw = await _read_buffer(proto)
        proto.response_bytes(200, JSON_HEADERS, _json_bytes({"bytes": len(raw)}))
    elif method == "POST" and path in {"/ingest/64k", "/ingest/2m"}:
        raw = await _read_buffer(proto)
        proto.response_bytes(200, JSON_HEADERS, _json_bytes({"bytes": len(raw)}))
    elif method == "POST" and path == "/ingest/stream/2m":
        total = await _read_stream(proto)
        proto.response_bytes(200, JSON_HEADERS, _json_bytes({"bytes": total}))
    elif method == "POST" and path == "/upload":
        raw = await _read_buffer(proto)
        proto.response_bytes(200, JSON_HEADERS, _json_bytes({"bytes": len(raw)}))
    else:
        proto.response_str(404, [], "Not Found")
