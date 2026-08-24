# pyright: reportMissingImports=false

import ujson

HELLO = "Hello, World!"
JSON_HEADERS = [("content-type", "application/json")]


async def app(scope, proto):
    if scope.proto != "http":
        return

    path, method = scope.path, scope.method

    if method == "GET" and path == "/plaintext":
        proto.response_str(
            200,
            [("content-type", "text/plain; charset=utf-8")],
            HELLO,
        )
    elif method == "GET" and path == "/json":
        proto.response_bytes(
            200,
            JSON_HEADERS,
            ujson.dumps({"message": HELLO}).encode(),
        )
    elif method == "GET" and path.startswith("/user/"):
        user_id = path.rsplit("/", 1)[-1]
        body = ujson.dumps({"id": user_id, "name": f"User {user_id}"})
        proto.response_bytes(200, JSON_HEADERS, body.encode())
    elif method == "POST" and path == "/validate":
        raw = await proto()
        data = ujson.loads(raw)
        name = data.get("name")
        age = data.get("age")
        if not isinstance(name, str) or not name:
            proto.response_bytes(
                400,
                JSON_HEADERS,
                ujson.dumps({"error": "name must be a non-empty string"}).encode(),
            )
            return
        if not isinstance(age, int) or age < 0 or age > 150:
            proto.response_bytes(
                400,
                JSON_HEADERS,
                ujson.dumps(
                    {"error": "age must be an integer between 0 and 150"}
                ).encode(),
            )
            return
        proto.response_bytes(
            200,
            JSON_HEADERS,
            ujson.dumps({"name": name, "age": age, "valid": True}).encode(),
        )
    else:
        proto.response_str(404, [], "Not Found")
