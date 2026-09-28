# pyright: reportMissingImports=false

import ujson
import falcon.asgi

from apps.common import (
    PLAINTEXT_BODY,
    REQUEST_HEADER,
    TEXT_CONTENT_TYPE_STR,
    as_str,
    bytes_line,
    json_echo_line,
    query_value,
    request_line,
    yield_once,
)


async def _read_all(req) -> bytes:
    return await req.stream.read()


async def _read_stream(req) -> int:
    total = 0
    async for chunk in req.stream:
        total += len(chunk)
    return total


def text(resp, line: str | bytes) -> None:
    resp.content_type = TEXT_CONTENT_TYPE_STR
    resp.data = line if isinstance(line, bytes) else line.encode("ascii")


class Plaintext:
    async def on_get(self, req, resp):
        text(resp, PLAINTEXT_BODY)


class UserResource:
    async def on_get(self, req, resp, user_id):
        text(
            resp,
            request_line(
                user_id,
                query_value(req.get_param("q")),
                as_str(
                    req.get_header("X-REQUEST-ID")
                    or req.get_header(REQUEST_HEADER)
                ),
            ),
        )


class EchoJson:
    async def on_post(self, req, resp):
        raw = await _read_all(req)
        await yield_once()
        text(resp, json_echo_line(ujson.loads(raw) if raw else {}))


class IngestBuffer:
    async def on_post(self, req, resp):
        body = await _read_all(req)
        await yield_once()
        text(resp, bytes_line(len(body)))


class IngestStream:
    async def on_post(self, req, resp):
        total = await _read_stream(req)
        await yield_once()
        text(resp, bytes_line(total))


class Upload:
    async def on_post(self, req, resp):
        body = await _read_all(req)
        await yield_once()
        text(resp, bytes_line(len(body)))


app = falcon.asgi.App()
app.add_route("/plaintext", Plaintext())
app.add_route("/user/{user_id}", UserResource())
app.add_route("/echo", EchoJson())
app.add_route("/ingest/64k", IngestBuffer())
app.add_route("/ingest/2m", IngestBuffer())
app.add_route("/ingest/stream/2m", IngestStream())
app.add_route("/upload", Upload())
