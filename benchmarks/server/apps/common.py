# pyright: reportMissingImports=false

"""Shared constants and helpers for benchmark apps.

Responses are plain text so JSON serializers cannot dominate a case. wrk
varies path, query, and header values; every app interpolates the same
`user=… q=… x=…` / `name=… age=…` / `bytes=…` lines.
"""

from __future__ import annotations

import asyncio

HELLO = "Hello, World!"
PLAINTEXT_BODY = HELLO.encode("utf-8")
TEXT_CONTENT_TYPE = b"text/plain; charset=utf-8"
TEXT_CONTENT_TYPE_STR = "text/plain; charset=utf-8"

# wrk cycles this many unique /user/{id}?q=… paths so lookup cannot collapse
# to one static URL (the compiled trie walks each param path).
PARAM_ID_COUNT = 4096
REQUEST_HEADER = "x-request-id"

PAYLOAD_1K = 1024
PAYLOAD_64K = 64 * 1024
PAYLOAD_2M = 2 * 1024 * 1024


def as_str(value: object, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("latin-1") if value else default
    if isinstance(value, (list, tuple)):
        return as_str(value[0], default) if value else default
    return str(value)


def query_value(value: object, default: str = "") -> str:
    return as_str(value, default)


def query_param(query_string: str | bytes | None, name: str = "q") -> str:
    """First `name=` from a raw query string (wrk values are unescaped)."""
    if not query_string:
        return ""
    if isinstance(query_string, bytes):
        query_string = query_string.decode("latin-1")
    prefix = name + "="
    for part in query_string.split("&"):
        if part.startswith(prefix):
            return part[len(prefix) :]
        if part == name:
            return ""
    return ""


def request_line(user_id: str, q: str, x_request_id: str) -> str:
    """Checkpoint line: path param `user_id`, query `q`, header `x-request-id`."""
    return f"user={user_id} q={q} x={x_request_id}"


def json_echo_line(data: object) -> str:
    """Checkpoint line from a small JSON object (`name`, `age`)."""
    body = data if isinstance(data, dict) else {}
    return f"name={body.get('name', '')} age={body.get('age', '')}"


def bytes_line(n: int) -> str:
    return f"bytes={n}"


async def yield_once() -> None:
    """One real event-loop suspension with no timer delay.

    Marks upload handlers as actually async (cache-hit backend / completed
    future) without capping throughput at 1 / sleep_seconds.
    """
    await asyncio.sleep(0)
