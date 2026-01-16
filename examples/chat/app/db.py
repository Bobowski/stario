"""
Stario Chat - Database Layer

SQLite-based persistence with connection pooling for async context.
Uses in-memory DB for development, file-based for production.

This module demonstrates passing database dependencies into handlers
using closures (Go-style dependency injection).
"""

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

from .state import Message, User


@dataclass
class Database:
    """
    SQLite wrapper with thread-local connections.

    SQLite connections aren't thread-safe, so we maintain one per thread.
    For production with multiple workers, each worker gets its own connection.
    """

    db_path: str
    _local: threading.local = field(default_factory=threading.local)

    def __post_init__(self):
        # Create tables on first init
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection, creating if needed."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def _cursor(self):
        """Context manager for cursor with auto-commit."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def _init_tables(self):
        """Create tables if they don't exist."""
        with self._cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    color TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    color TEXT NOT NULL,
                    typing INTEGER NOT NULL DEFAULT 0
                )
            """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp)
            """
            )

    # =========================================================================
    # Message Operations
    # =========================================================================

    def add_message(self, msg: Message) -> None:
        """Insert a new message."""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (id, user_id, username, color, text, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (msg.id, msg.user_id, msg.username, msg.color, msg.text, msg.timestamp),
            )
            # Keep only last 100 messages
            cur.execute(
                """
                DELETE FROM messages WHERE id NOT IN (
                    SELECT id FROM messages ORDER BY timestamp DESC LIMIT 100
                )
            """
            )

    def get_messages(self, limit: int = 100) -> list[Message]:
        """Fetch recent messages, oldest first."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, username, color, text, timestamp
                FROM messages ORDER BY timestamp ASC LIMIT ?
                """,
                (limit,),
            )
            return [
                Message(
                    id=row["id"],
                    user_id=row["user_id"],
                    username=row["username"],
                    color=row["color"],
                    text=row["text"],
                    timestamp=row["timestamp"],
                )
                for row in cur.fetchall()
            ]

    # =========================================================================
    # User Operations
    # =========================================================================

    def add_user(self, user: User) -> None:
        """Add or update a user (upsert)."""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO users (id, username, color, typing)
                VALUES (?, ?, ?, ?)
                """,
                (user.id, user.username, user.color, int(user.typing)),
            )

    def remove_user(self, user_id: str) -> None:
        """Remove a user by ID."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def get_user(self, user_id: str) -> User | None:
        """Get a user by ID, or None if not found."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, username, color, typing FROM users WHERE id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return User(
                id=row["id"],
                username=row["username"],
                color=row["color"],
                typing=bool(row["typing"]),
            )

    def get_users(self) -> dict[str, User]:
        """Get all online users as a dict keyed by user_id."""
        with self._cursor() as cur:
            cur.execute("SELECT id, username, color, typing FROM users")
            return {
                row["id"]: User(
                    id=row["id"],
                    username=row["username"],
                    color=row["color"],
                    typing=bool(row["typing"]),
                )
                for row in cur.fetchall()
            }

    def user_exists(self, user_id: str) -> bool:
        """Check if a user exists."""
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE id = ?", (user_id,))
            return cur.fetchone() is not None

    def set_user_typing(self, user_id: str, typing: bool) -> bool:
        """
        Update user's typing status. Returns True if state changed.
        """
        with self._cursor() as cur:
            cur.execute("SELECT typing FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
            if row is None:
                return False

            current_typing = bool(row["typing"])
            if current_typing == typing:
                return False

            cur.execute(
                "UPDATE users SET typing = ? WHERE id = ?",
                (int(typing), user_id),
            )
            return True


def create_database(is_dev: bool = True) -> Database:
    """
    Factory function to create appropriate database.

    Args:
        is_dev: If True, use in-memory SQLite. Otherwise, use file-based.

    Returns:
        Configured Database instance with tables created.
    """
    if is_dev:
        # In-memory DB - fast, ephemeral, perfect for development
        # Note: Each process gets its own memory DB
        db_path = ":memory:"
    else:
        # File-based DB - persists across restarts
        db_path = "chat.db"

    return Database(db_path=db_path)
