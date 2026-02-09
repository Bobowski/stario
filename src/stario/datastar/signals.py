import json
from typing import Any, NotRequired, TypedDict

from stario.http.request import Request

# Signal value types that Datastar supports
type SignalValue = str | int | float | bool | list[Any] | dict[str, Any]
type SignalsDict = dict[str, SignalValue]


async def get_signals(r: Request) -> SignalsDict:
    """
    Extract Datastar signals from request.

    For GET requests, signals are in query params under 'datastar'.
    For POST requests, signals are in the JSON body.
    """
    if r.method == "GET":
        data = r.query.get("datastar")
        if data is not None:
            result = json.loads(data)
            if isinstance(result, dict):
                return result
        return {}

    body = await r.body()
    if body:
        result = json.loads(body)
        if isinstance(result, dict):
            return result
    return {}


class FileSignal(TypedDict):
    """
    File upload signal - Datastar encodes files as base64.

    Attributes:
        name: Original filename
        contents: Base64-encoded file data
        mime: MIME type (optional)
    """

    name: str
    contents: str
    mime: NotRequired[
        str
    ]  # NotRequired - this key is optional in the FileSignal TypedDict
    # _decoded: bytes | None = None

    # def decode(self) -> bytes:
    #     """Decode the base64 contents to raw bytes."""
    #     if self._decoded is None:
    #         self._decoded = base64.b64decode(self.contents)
    #     return self._decoded

    # def size(self) -> int:
    #     """Get decoded file size in bytes."""
    #     return len(self.decode())
