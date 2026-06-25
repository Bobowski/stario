"""
Thin SQLite core — connection and transactions only.

Features own their tables: each feature's `data.py` exports a `SCHEMA`
(applied by bootstrap) and plain query functions that take a `Database`.
This file never grows when you add a feature.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager


class Database:
    """One SQLite connection — Stario serves on a single event-loop thread."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor]:
        """One transaction per block — commit on success, rollback on error."""
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
        self._conn.executescript(ddl)
