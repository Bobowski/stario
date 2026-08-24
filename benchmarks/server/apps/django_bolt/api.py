import msgspec
from django_bolt import BoltAPI
from django_bolt.responses import PlainText

from apps.common import validate_fields

HELLO = "Hello, World!"
api = BoltAPI()


@api.get("/plaintext")
async def plaintext():
    return PlainText(HELLO)


@api.get("/json")
async def json_endpoint():
    return {"message": HELLO}


@api.get("/user/{user_id}")
async def get_user(user_id: str):
    return {"id": user_id, "name": f"User {user_id}"}


class UserInput(msgspec.Struct):
    name: str
    age: int


@api.post("/validate")
async def validate(body: UserInput):
    payload, status = validate_fields({"name": body.name, "age": body.age})
    if status != 200:
        return payload, status
    return payload


@api.post("/form")
async def post_form(request):
    _ = request.body
    return PlainText("", status_code=204)


@api.post("/echo/json")
async def post_echo_json(request):
    return {"bytes": len(request.body)}


@api.post("/ingest/64k")
@api.post("/ingest/2m")
@api.post("/ingest/stream/2m")
async def ingest_buffer(request):
    return {"bytes": len(request.body)}


@api.post("/upload")
async def upload(request):
    return {"bytes": len(request.body)}
