# pyright: reportMissingImports=false

import argparse

from sanic import Sanic, json, text

HELLO = "Hello, World!"

app = Sanic("stario_benchmark_sanic")
app.config.ACCESS_LOG = False
app.config.RESPONSE_TIMEOUT = 60
app.config.REQUEST_TIMEOUT = 60


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
    body = request.json or {}
    name = body.get("name")
    age = body.get("age")

    if not isinstance(name, str) or not name:
        return json({"error": "name must be a non-empty string"}, status=400)
    if not isinstance(age, int) or age < 0 or age > 150:
        return json(
            {"error": "age must be an integer between 0 and 150"},
            status=400,
        )

    return json({"name": name, "age": age, "valid": True})


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
