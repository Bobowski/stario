"""
Room domain data — schema and queries for rooms, messages, and presence.

The feature owns its tables. Bootstrap applies `SCHEMA` once; every query
is a plain function taking the shared `Database`. Query names follow the
SQL verb: `list_*` returns many, `get_*` returns one or `None`,
`add_*` / `remove_*` / `set_*` mutate.
"""

import re

from app.db import Database

from .models import Message, Room, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    color TEXT NOT NULL,
    text TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    username TEXT NOT NULL,
    color TEXT NOT NULL,
    typing INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id, room_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_room_time
ON messages(room_id, timestamp);
"""

MESSAGE_HISTORY_LIMIT = 100

# --- Rooms ---


def list_rooms(db: Database) -> list[Room]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT id, title, description FROM rooms ORDER BY title COLLATE NOCASE"
        )
        return [
            Room(id=row["id"], title=row["title"], description=row["description"])
            for row in cur.fetchall()
        ]


def get_room(db: Database, room_id: str) -> Room | None:
    with db.transaction() as cur:
        cur.execute(
            "SELECT id, title, description FROM rooms WHERE id = ?",
            (room_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return Room(id=row["id"], title=row["title"], description=row["description"])


def add_room(db: Database, *, title: str, description: str) -> Room:
    """Insert a room; `id` is slugified from `title` (unique)."""
    room = Room(id=_unique_room_id(db, title), title=title, description=description)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO rooms (id, title, description) VALUES (?, ?, ?)",
            (room.id, room.title, room.description),
        )
    return room


def delete_room(db: Database, room_id: str) -> bool:
    """Remove the room and everything scoped to it (messages, presence)."""
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,))
        if cur.fetchone() is None:
            return False
        cur.execute("DELETE FROM messages WHERE room_id = ?", (room_id,))
        cur.execute("DELETE FROM users WHERE room_id = ?", (room_id,))
        cur.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        return True


# --- Messages ---


def add_message(db: Database, msg: Message) -> None:
    """Store a message and trim the room to the newest history window."""
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO messages
                (id, room_id, user_id, username, color, text, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id,
                msg.room_id,
                msg.user_id,
                msg.username,
                msg.color,
                msg.text,
                msg.timestamp,
            ),
        )
        cur.execute(
            """
            DELETE FROM messages
            WHERE room_id = ? AND id NOT IN (
                SELECT id FROM messages
                WHERE room_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (msg.room_id, msg.room_id, MESSAGE_HISTORY_LIMIT),
        )


def list_messages(
    db: Database, room_id: str, limit: int = MESSAGE_HISTORY_LIMIT
) -> list[Message]:
    with db.transaction() as cur:
        cur.execute(
            """
            SELECT id, room_id, user_id, username, color, text, timestamp
            FROM messages
            WHERE room_id = ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (room_id, limit),
        )
        return [
            Message(
                id=row["id"],
                room_id=row["room_id"],
                user_id=row["user_id"],
                username=row["username"],
                color=row["color"],
                text=row["text"],
                timestamp=row["timestamp"],
            )
            for row in cur.fetchall()
        ]


# --- Presence ---


def add_user(db: Database, user: User) -> None:
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT OR REPLACE INTO users (id, room_id, username, color, typing)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user.id, user.room_id, user.username, user.color, int(user.typing)),
        )


def remove_user(db: Database, user_id: str, room_id: str) -> None:
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM users WHERE id = ? AND room_id = ?",
            (user_id, room_id),
        )


def list_users(db: Database, room_id: str) -> list[User]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT id, room_id, username, color, typing FROM users WHERE room_id = ?",
            (room_id,),
        )
        return [
            User(
                id=row["id"],
                room_id=row["room_id"],
                username=row["username"],
                color=row["color"],
                typing=bool(row["typing"]),
            )
            for row in cur.fetchall()
        ]


def count_users_by_room(db: Database) -> dict[str, int]:
    """Online counts for all rooms in one query — no per-room round trips."""
    with db.transaction() as cur:
        cur.execute("SELECT room_id, COUNT(*) AS n FROM users GROUP BY room_id")
        return {row["room_id"]: int(row["n"]) for row in cur.fetchall()}


def user_exists(db: Database, user_id: str, room_id: str) -> bool:
    with db.transaction() as cur:
        cur.execute(
            "SELECT 1 FROM users WHERE id = ? AND room_id = ?",
            (user_id, room_id),
        )
        return cur.fetchone() is not None


def set_user_typing(db: Database, user_id: str, room_id: str, typing: bool) -> bool:
    """Flip the typing flag; returns True only when the value changed."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT typing FROM users WHERE id = ? AND room_id = ?",
            (user_id, room_id),
        )
        row = cur.fetchone()
        if row is None or bool(row["typing"]) == typing:
            return False
        cur.execute(
            "UPDATE users SET typing = ? WHERE id = ? AND room_id = ?",
            (int(typing), user_id, room_id),
        )
        return True


# --- Helpers ---


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "room"


def _unique_room_id(db: Database, title: str) -> str:
    base = _slugify(title)
    candidate = base
    n = 2
    while get_room(db, candidate) is not None:
        candidate = f"{base}-{n}"
        n += 1
    return candidate
