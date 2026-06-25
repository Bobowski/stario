"""Demo visitor identity — one id per browser tab via Datastar signals and sessionStorage."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass

from stario import Context
from stario.datastar import read_signals

SESSION_STORAGE_KEY = "chat_visitor"

# Restore a tab's identity on full page loads; each tab has its own sessionStorage.
VISITOR_SESSION_INIT = f"""
(() => {{
  const key = {json.dumps(SESSION_STORAGE_KEY)};
  try {{
    const raw = sessionStorage.getItem(key);
    if (raw) {{
      const v = JSON.parse(raw);
      if (v.user_id) {{
        $user_id = v.user_id;
        $username = v.username;
        $color = v.color;
      }}
    }} else if ($user_id) {{
      sessionStorage.setItem(
        key,
        JSON.stringify({{ user_id: $user_id, username: $username, color: $color }}),
      );
    }}
  }} catch {{}}
}})();
"""

ADJECTIVES = (
    "Happy",
    "Sleepy",
    "Grumpy",
    "Sneezy",
    "Bashful",
    "Dopey",
    "Doc",
    "Swift",
    "Clever",
    "Brave",
    "Gentle",
    "Mighty",
    "Sneaky",
    "Jolly",
    "Fuzzy",
)

ANIMALS = (
    "Panda",
    "Fox",
    "Owl",
    "Cat",
    "Dog",
    "Bear",
    "Wolf",
    "Tiger",
    "Lion",
    "Koala",
    "Bunny",
    "Penguin",
    "Otter",
    "Seal",
    "Duck",
    "Frog",
    "Sloth",
    "Deer",
    "Moose",
    "Falcon",
)

COLORS = (
    "#e74c3c",
    "#e67e22",
    "#f1c40f",
    "#2ecc71",
    "#1abc9c",
    "#3498db",
    "#9b59b6",
    "#e91e63",
    "#00bcd4",
    "#8bc34a",
)


@dataclass(frozen=True, slots=True)
class VisitorIdentity:
    user_id: str
    username: str
    color: str


def mint_identity() -> VisitorIdentity:
    return VisitorIdentity(
        user_id=str(uuid.uuid4())[:8],
        username=generate_username(),
        color=generate_color(),
    )


def generate_username() -> str:
    return f"{random.choice(ADJECTIVES)}{random.choice(ANIMALS)}"


def generate_color() -> str:
    return random.choice(COLORS)


def identity_from_query(c: Context) -> VisitorIdentity | None:
    user_id = c.req.query.get("user_id", "")
    if not user_id:
        return None
    username = c.req.query.get("username", "")
    color = c.req.query.get("color", "")
    if not username or not color:
        return None
    return VisitorIdentity(user_id=user_id, username=username, color=color)


async def identity_from_signals(c: Context) -> VisitorIdentity | None:
    payload = await read_signals(c.req)
    user_id = str(payload.get("user_id", ""))
    if not user_id:
        return None
    username = str(payload.get("username", ""))
    color = str(payload.get("color", ""))
    if not username or not color:
        return None
    return VisitorIdentity(user_id=user_id, username=username, color=color)


async def resolve_identity(c: Context) -> VisitorIdentity | None:
    """Read the tab's identity from Datastar signals (set by the client)."""
    return await identity_from_signals(c)


def identity_for_page(c: Context) -> VisitorIdentity:
    """First paint: optional query override, otherwise mint a fresh tab identity."""
    return identity_from_query(c) or mint_identity()
