"""
Env-first configuration — read once in bootstrap, passed down explicitly.

Environment variables are the runtime source of truth; defaults here keep
local development zero-setup. No scattered `os.environ` reads elsewhere.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Config:
    db_path: str = ":memory:"

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            db_path=os.environ.get("CHAT_DB_PATH", ":memory:"),
        )
