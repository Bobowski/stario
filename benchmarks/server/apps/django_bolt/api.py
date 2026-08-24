import msgspec
from django_bolt import BoltAPI
from django_bolt.responses import PlainText

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
    if not body.name:
        return {"error": "name must be a non-empty string"}, 400
    if body.age < 0 or body.age > 150:
        return {"error": "age must be an integer between 0 and 150"}, 400
    return {"name": body.name, "age": body.age, "valid": True}
