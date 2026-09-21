# pyright: reportMissingImports=false

"""Shared constants and helpers for benchmark apps."""

from __future__ import annotations

HELLO = "Hello, World!"
JSON_CONTENT_TYPE = b"application/json"
FORM_BODY = b"name=Ada&age=42&source=benchmark"

PAYLOAD_1K = 1024
PAYLOAD_64K = 64 * 1024
PAYLOAD_2M = 2 * 1024 * 1024


def validate_fields(data: dict) -> tuple[dict, int]:
    name = data.get("name")
    age = data.get("age")
    if not isinstance(name, str) or not name:
        return {"error": "name must be a non-empty string"}, 400
    if not isinstance(age, int) or age < 0 or age > 150:
        return {"error": "age must be an integer between 0 and 150"}, 400
    return {"name": name, "age": age, "valid": True}, 200


def byte_count(value: int) -> dict[str, int]:
    return {"bytes": value}
