"""Room page signals — identity plus the message input."""

from dataclasses import dataclass

from stario import Context
from stario.datastar import read_signals


@dataclass(frozen=True, slots=True)
class ChatSignals:
    user_id: str = ""
    username: str = ""
    color: str = ""
    message: str = ""


async def read_chat_signals(c: Context) -> ChatSignals:
    payload = await read_signals(c.req)
    return ChatSignals(
        user_id=str(payload.get("user_id", "")),
        username=str(payload.get("username", "")),
        color=str(payload.get("color", "")),
        message=str(payload.get("message", "")),
    )
