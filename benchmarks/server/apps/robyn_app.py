# pyright: reportMissingImports=false

import os

from robyn import Config, Robyn

config = Config()
app = Robyn(__file__, config=config)

HELLO = "Hello, World!"


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
    body = request.json() or {}
    name = body.get("name")
    age = body.get("age")
    if not isinstance(name, str) or not name:
        return {"error": "name must be a non-empty string"}, 400
    if not isinstance(age, int) or age < 0 or age > 150:
        return {"error": "age must be an integer between 0 and 150"}, 400
    return {"name": name, "age": age, "valid": True}


if __name__ == "__main__":
    host = os.environ.get("BENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCH_PORT", "8080"))
    app.start(host=host, port=port, _check_port=False)
