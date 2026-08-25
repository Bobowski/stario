# pyright: reportMissingImports=false

"""aiohttp production bench: uvloop + SO_REUSEPORT.

The runner starts ``BENCH_PROCS`` processes of this file. Each process binds
the same port with ``reuse_port=True`` (HttpArena / production aiohttp).
"""

import argparse
import asyncio
import os

import ujson
import uvloop
from aiohttp import web

from apps.common import validate_fields

HELLO = "Hello, World!"
JSON_CONTENT_TYPE = "application/json"
MAX_BODY = 4 * 1024 * 1024


def json_response(value: object, status: int = 200) -> web.Response:
    return web.Response(
        body=ujson.dumps(value).encode("utf-8"),
        status=status,
        content_type=JSON_CONTENT_TYPE,
    )


async def plaintext(_request: web.Request) -> web.Response:
    return web.Response(text=HELLO, content_type="text/plain")


async def json_endpoint(_request: web.Request) -> web.Response:
    return json_response({"message": HELLO})


async def get_user(request: web.Request) -> web.Response:
    user_id = request.match_info["user_id"]
    return json_response({"id": user_id, "name": f"User {user_id}"})


async def validate(request: web.Request) -> web.Response:
    payload, status = validate_fields(ujson.loads(await request.read()))
    return json_response(payload, status)


async def post_form(request: web.Request) -> web.Response:
    await request.read()
    return web.Response(status=204)


async def post_echo_json(request: web.Request) -> web.Response:
    body = await request.read()
    return json_response({"bytes": len(body)})


async def ingest_buffer(request: web.Request) -> web.Response:
    body = await request.read()
    return json_response({"bytes": len(body)})


async def ingest_stream(request: web.Request) -> web.Response:
    total = 0
    async for chunk in request.content.iter_any():
        total += len(chunk)
    return json_response({"bytes": total})


async def upload(request: web.Request) -> web.Response:
    body = await request.read()
    return json_response({"bytes": len(body)})


def build_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/plaintext", plaintext)
    app.router.add_get("/json", json_endpoint)
    app.router.add_get("/user/{user_id}", get_user)
    app.router.add_post("/validate", validate)
    app.router.add_post("/form", post_form)
    app.router.add_post("/echo/json", post_echo_json)
    app.router.add_post("/ingest/64k", ingest_buffer)
    app.router.add_post("/ingest/2m", ingest_buffer)
    app.router.add_post("/ingest/stream/2m", ingest_stream)
    app.router.add_post("/upload", upload)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("BENCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BENCH_PORT", "3000")))
    args = parser.parse_args()
    reuse_port = os.environ.get("AIOHTTP_REUSE_PORT", "1") == "1"

    uvloop.install()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = web.AppRunner(build_app(), access_log=None)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(
        runner,
        args.host,
        args.port,
        reuse_port=reuse_port,
        backlog=4096,
        shutdown_timeout=1.0,
    )
    loop.run_until_complete(site.start())
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(runner.cleanup())


if __name__ == "__main__":
    main()
