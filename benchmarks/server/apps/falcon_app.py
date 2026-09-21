# pyright: reportMissingImports=false

import ujson
import falcon.asgi

from apps.common import validate_fields

HELLO = "Hello, World!"
JSON_MEDIA_TYPE = falcon.MEDIA_JSON


def json_body(resp, value, status=falcon.HTTP_200):
    resp.status = status
    resp.content_type = JSON_MEDIA_TYPE
    resp.data = ujson.dumps(value).encode("utf-8")


async def _read_all(req) -> bytes:
    return await req.stream.read()


async def _read_stream(req) -> int:
    total = 0
    async for chunk in req.stream:
        total += len(chunk)
    return total


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
        body = ujson.loads(await _read_all(req))
        payload, status = validate_fields(body)
        json_body(
            resp,
            payload,
            falcon.HTTP_400 if status == 400 else falcon.HTTP_200,
        )


class FormResource:
    async def on_post(self, req, resp):
        await _read_all(req)
        resp.status = falcon.HTTP_204


class EchoJson:
    async def on_post(self, req, resp):
        body = await _read_all(req)
        json_body(resp, {"bytes": len(body)})


class IngestBuffer:
    async def on_post(self, req, resp):
        body = await _read_all(req)
        json_body(resp, {"bytes": len(body)})


class IngestStream:
    async def on_post(self, req, resp):
        json_body(resp, {"bytes": await _read_stream(req)})


class Upload:
    async def on_post(self, req, resp):
        body = await _read_all(req)
        json_body(resp, {"bytes": len(body)})


app = falcon.asgi.App()
app.add_route("/plaintext", Plaintext())
app.add_route("/json", JsonResource())
app.add_route("/user/{user_id}", UserResource())
app.add_route("/validate", ValidateResource())
app.add_route("/form", FormResource())
app.add_route("/echo/json", EchoJson())
app.add_route("/ingest/64k", IngestBuffer())
app.add_route("/ingest/2m", IngestBuffer())
app.add_route("/ingest/stream/2m", IngestStream())
app.add_route("/upload", Upload())
