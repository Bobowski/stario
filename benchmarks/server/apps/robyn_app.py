# pyright: reportMissingImports=false

import os

import ujson
from robyn import Config, Robyn
from robyn.jsonify import jsonify
from robyn.robyn import Headers, Response

from apps.common import validate_fields

config = Config()
app = Robyn(__file__, config=config)

HELLO = "Hello, World!"


def _body_bytes(request) -> bytes:
    body = request.body
    if isinstance(body, str):
        return body.encode("utf-8")
    return body or b""


@app.get("/plaintext")
async def plaintext():
    return HELLO


@app.get("/json")
async def json_endpoint():
    return {"message": HELLO}


@app.get("/user/:user_id")
async def get_user(request):
    user_id = request.path_params["user_id"]
    return {"id": user_id, "name": f"User {user_id}"}


@app.post("/validate")
async def validate(request):
    raw = _body_bytes(request)
    payload, status = validate_fields(ujson.loads(raw) if raw else {})
    if status != 200:
        return Response(
            status_code=status,
            headers=Headers({"Content-Type": "application/json"}),
            description=jsonify(payload),
        )
    return payload


@app.post("/form")
async def post_form(request):
    _body_bytes(request)
    return "", 204


@app.post("/echo/json")
async def post_echo_json(request):
    body = _body_bytes(request)
    return {"bytes": len(body)}


@app.post("/ingest/64k")
@app.post("/ingest/2m")
async def ingest_buffer(request):
    body = _body_bytes(request)
    return {"bytes": len(body)}


@app.post("/ingest/stream/2m")
async def ingest_stream(request):
    body = _body_bytes(request)
    return {"bytes": len(body)}


@app.post("/upload")
async def upload(request):
    body = _body_bytes(request)
    return {"bytes": len(body)}


if __name__ == "__main__":
    host = os.environ.get("BENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCH_PORT", "8080"))
    app.start(host=host, port=port, _check_port=False)
