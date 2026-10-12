"""
Thin SQLite core — connection and transactions only.

Features own their tables: each feature's `data.py` exports a `SCHEMA`
(applied by bootstrap) and plain query functions that take a `Database`.
This file never grows when you add a feature.
"""

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager


class Database:
    """SQLite connection shared across worker threads (one lock per transaction)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor]:
        """One transaction per block — commit on success, rollback on error."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def apply_schema(self, ddl: str) -> None:
        """Run a feature's `SCHEMA` script. Idempotent DDL only."""
        with self._lock:
            self._conn.executescript(ddl)
