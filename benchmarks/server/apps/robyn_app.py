# pyright: reportMissingImports=false

import argparse

import ujson
from robyn import Robyn

HELLO = "Hello, World!"
JSON_HEADERS = {"Content-Type": "application/json"}


def error_json(value):
    return (value, JSON_HEADERS, 400)


app = Robyn(__file__)


@app.get("/plaintext", const=True)
def plaintext():
    return HELLO


@app.get("/json", const=True)
def json_endpoint():
    return {"message": HELLO}


@app.get("/user/:user_id")
def get_user(user_id: str):
    return {"id": user_id, "name": f"User {user_id}"}


@app.post("/validate")
def validate(request):
    body = ujson.loads(request.body)
    name = body.get("name")
    age = body.get("age")

    if not isinstance(name, str) or not name:
        return error_json({"error": "name must be a non-empty string"})
    if not isinstance(age, int) or age < 0 or age > 150:
        return error_json({"error": "age must be an integer between 0 and 150"})

    return {"name": name, "age": age, "valid": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args, _ = parser.parse_known_args()
    app.start(host=args.host, port=args.port, _check_port=False)
