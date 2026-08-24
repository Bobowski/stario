# pyright: reportMissingImports=false

import argparse

from sanic import Sanic, empty, json, text

from apps.common import validate_fields

HELLO = "Hello, World!"

app = Sanic("stario_benchmark_sanic")
app.config.ACCESS_LOG = False
app.config.RESPONSE_TIMEOUT = 120
app.config.REQUEST_TIMEOUT = 120
app.config.REQUEST_MAX_SIZE = 4 * 1024 * 1024


@app.get("/plaintext")
async def plaintext(request):
    return text(HELLO)


@app.get("/json")
async def json_endpoint(request):
    return json({"message": HELLO})


@app.get("/user/<user_id>")
async def get_user(request, user_id: str):
    return json({"id": user_id, "name": f"User {user_id}"})


@app.post("/validate")
async def validate(request):
    payload, status = validate_fields(request.json or {})
    return json(payload, status=status)


@app.post("/form")
async def post_form(request):
    await request.read()
    return empty()


@app.post("/echo/json")
async def post_echo_json(request):
    body = await request.read()
    return json({"bytes": len(body)})


@app.post("/ingest/64k", name="ingest_64k")
@app.post("/ingest/2m", name="ingest_2m")
async def ingest_buffer(request):
    body = await request.read()
    return json({"bytes": len(body)})


@app.post("/ingest/stream/2m", name="ingest_stream_2m")
async def ingest_stream(request):
    total = 0
    async for chunk in request.stream:
        total += len(chunk)
    return json({"bytes": total})


@app.post("/upload")
async def upload(request):
    body = await request.read()
    return json({"bytes": len(body)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    app.run(
        host=args.host,
        port=args.port,
        single_process=True,
        access_log=False,
        debug=False,
    )
