# pyright: reportMissingImports=false

import ujson
import falcon.asgi

HELLO = "Hello, World!"
JSON_MEDIA_TYPE = falcon.MEDIA_JSON


def json_body(resp, value, status=falcon.HTTP_200):
    resp.status = status
    resp.content_type = JSON_MEDIA_TYPE
    resp.data = ujson.dumps(value).encode("utf-8")


class Plaintext:
    async def on_get(self, req, resp):
        resp.text = HELLO


class JsonResource:
    async def on_get(self, req, resp):
        json_body(resp, {"message": HELLO})


class UserResource:
    async def on_get(self, req, resp, user_id):
        json_body(resp, {"id": user_id, "name": f"User {user_id}"})


class ValidateResource:
    async def on_post(self, req, resp):
        body = ujson.loads(await req.stream.read())
        name = body.get("name")
        age = body.get("age")

        if not isinstance(name, str) or not name:
            json_body(resp, {"error": "name must be a non-empty string"}, falcon.HTTP_400)
            return
        if not isinstance(age, int) or age < 0 or age > 150:
            json_body(
                resp,
                {"error": "age must be an integer between 0 and 150"},
                falcon.HTTP_400,
            )
            return

        json_body(resp, {"name": name, "age": age, "valid": True})


app = falcon.asgi.App()
app.add_route("/plaintext", Plaintext())
app.add_route("/json", JsonResource())
app.add_route("/user/{user_id}", UserResource())
app.add_route("/validate", ValidateResource())
