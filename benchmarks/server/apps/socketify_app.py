# pyright: reportMissingImports=false

import os

from socketify import App, AppListenOptions

HELLO = "Hello, World!"
app = App()


def plaintext(res, req):
    res.end(HELLO)


def json_endpoint(res, req):
    res.end({"message": HELLO})


def get_user(res, req):
    user_id = req.get_parameter(0)
    res.end({"id": user_id, "name": f"User {user_id}"})


async def validate(res, req):
    body = await res.get_json() or {}
    name = body.get("name")
    age = body.get("age")
    if not isinstance(name, str) or not name:
        return res.write_status(400).end(
            {"error": "name must be a non-empty string"}
        )
    if not isinstance(age, int) or age < 0 or age > 150:
        return res.write_status(400).end(
            {"error": "age must be an integer between 0 and 150"}
        )
    res.end({"name": name, "age": age, "valid": True})


app.get("/plaintext", plaintext)
app.get("/json", json_endpoint)
app.get("/user/:user_id", get_user)
app.post("/validate", validate)

host = os.environ.get("BENCH_HOST", "127.0.0.1")
port = int(os.environ.get("BENCH_PORT", "3000"))
app.listen(AppListenOptions(port=port, host=host), lambda _cfg: None)
app.run()
