# pyright: reportMissingImports=false

"""Litestar production bench: uvicorn --workers N --loop uvloop --http httptools.

Handlers return fresh values every request. JSON uses Litestar's default
msgspec path (HttpArena / production). OpenAPI and compression stay off so
the comparison matches our other framework rows.
"""

from litestar import Litestar, MediaType, Request, Response, get, post
from litestar.status_codes import HTTP_204_NO_CONTENT

from apps.common import validate_fields

HELLO = "Hello, World!"
MAX_BODY = 4 * 1024 * 1024


@get("/plaintext", media_type=MediaType.TEXT, sync_to_thread=False)
def plaintext() -> str:
    return HELLO


@get("/json", sync_to_thread=False)
def json_endpoint() -> dict:
    return {"message": HELLO}


@get("/user/{user_id:str}", sync_to_thread=False)
def get_user(user_id: str) -> dict:
    return {"id": user_id, "name": f"User {user_id}"}


@post("/validate")
async def validate(request: Request) -> Response:
    payload, status = validate_fields((await request.json()) or {})
    return Response(payload, status_code=status)


@post("/form", status_code=HTTP_204_NO_CONTENT)
async def post_form(request: Request) -> None:
    await request.body()


@post("/echo/json")
async def post_echo_json(request: Request) -> dict:
    body = await request.body()
    return {"bytes": len(body)}


@post("/ingest/64k")
async def ingest_64k(request: Request) -> dict:
    body = await request.body()
    return {"bytes": len(body)}


@post("/ingest/2m")
async def ingest_2m(request: Request) -> dict:
    body = await request.body()
    return {"bytes": len(body)}


@post("/ingest/stream/2m")
async def ingest_stream(request: Request) -> dict:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    return {"bytes": total}


@post("/upload")
async def upload(request: Request) -> dict:
    body = await request.body()
    return {"bytes": len(body)}


app = Litestar(
    route_handlers=[
        plaintext,
        json_endpoint,
        get_user,
        validate,
        post_form,
        post_echo_json,
        ingest_64k,
        ingest_2m,
        ingest_stream,
        upload,
    ],
    request_max_body_size=MAX_BODY,
    openapi_config=None,
)
