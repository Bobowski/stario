# pyright: reportMissingImports=false

try:
    import ujson
except ImportError:  # pragma: no cover - bench venvs install ujson
    import json as ujson

from apps.common import (
    PLAINTEXT_BODY,
    REQUEST_HEADER,
    TEXT_CONTENT_TYPE,
    as_str,
    bytes_line,
    json_echo_line,
    request_line,
    yield_once,
)
from stario import App, Route, Span


def text(w, line: str) -> None:
    w.respond(line.encode("ascii"), TEXT_CONTENT_TYPE)


async def plaintext(c, w):
    w.respond(PLAINTEXT_BODY, TEXT_CONTENT_TYPE)


async def read_request(c, w):
    text(
        w,
        request_line(
            c.match.params["user_id"],
            c.req.query.get("q") or "",
            as_str(c.req.headers.get(REQUEST_HEADER)),
        ),
    )


async def post_json(c, w):
    raw = await c.req.body()
    await yield_once()
    text(w, json_echo_line(ujson.loads(raw) if raw else {}))


async def ingest_buffer(c, w):
    body = await c.req.body()
    await yield_once()
    text(w, bytes_line(len(body)))


async def ingest_stream(c, w):
    total = 0
    async for chunk in c.req.stream():
        total += len(chunk)
    await yield_once()
    text(w, bytes_line(total))


async def upload(c, w):
    body = await c.req.body()
    await yield_once()
    text(w, bytes_line(len(body)))


async def bootstrap(app: App, span: Span) -> None:
    app.add(Route("GET /plaintext"), plaintext)
    app.add(Route("GET /user/{user_id}"), read_request)
    app.add(Route("POST /echo"), post_json)
    app.add(Route("POST /ingest/64k"), ingest_buffer)
    app.add(Route("POST /ingest/2m"), ingest_buffer)
    app.add(Route("POST /ingest/stream/2m"), ingest_stream)
    app.add(Route("POST /upload"), upload)
    yield
