# pyright: reportMissingImports=false

"""Pyronova production bench: one process, PYRONOVA_WORKERS sub-interpreters.

Honest handlers only — no ``add_fast_response``, no JSON byte cache, no
``drain_count()``. JSON is a fresh dict each request (framework serde).
Validate uses the stdlib decoder so the route can stay on a sub-interpreter
(orjson / ujson need ``gil=True`` under PEP 684).
"""

import json
import os

from pyronova import Pyronova, Request, Response

from apps.common import validate_fields

HELLO = "Hello, World!"
MAX_BODY = 4 * 1024 * 1024

app = Pyronova()
app.max_body_size = MAX_BODY


@app.get("/plaintext")
def plaintext(_req: Request):
    return Response(HELLO, content_type="text/plain")


@app.get("/json")
def json_endpoint(_req: Request):
    return {"message": HELLO}


@app.get("/user/{user_id}")
def get_user(req: Request):
    user_id = req.params["user_id"]
    return {"id": user_id, "name": f"User {user_id}"}


@app.post("/validate")
def validate(req: Request):
    raw = req.body or b""
    payload, status = validate_fields(json.loads(raw) if raw else {})
    return Response(
        json.dumps(payload),
        status_code=status,
        content_type="application/json",
    )


@app.post("/form")
def post_form(req: Request):
    _ = req.body
    return Response("", status_code=204)


@app.post("/echo/json")
def post_echo_json(req: Request):
    return {"bytes": len(req.body or b"")}


@app.post("/ingest/64k")
def ingest_64k(req: Request):
    return {"bytes": len(req.body or b"")}


@app.post("/ingest/2m")
def ingest_2m(req: Request):
    return {"bytes": len(req.body or b"")}


@app.post("/ingest/stream/2m", stream=True)
def ingest_stream(req: Request):
    total = 0
    for chunk in req.stream:
        total += len(chunk)
    return {"bytes": total}


@app.post("/upload")
def upload(req: Request):
    return {"bytes": len(req.body or b"")}


if __name__ == "__main__":
    host = os.environ.get("BENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCH_PORT", "3000"))
    app.run(host=host, port=port)
