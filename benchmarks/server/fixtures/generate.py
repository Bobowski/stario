#!/usr/bin/env python3
"""Generate binary and JSON payload fixtures for upload benchmarks."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent


def _pattern(size: int) -> bytes:
    return bytes(i % 256 for i in range(size))


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, size in (
        ("payload-1k.bin", 1024),
        ("payload-64k.bin", 64 * 1024),
        ("payload-2m.bin", 2 * 1024 * 1024),
    ):
        path = FIXTURES / name
        if not path.exists() or path.stat().st_size != size:
            path.write_bytes(_pattern(size))

    json_path = FIXTURES / "payload-1k.json"
    payload = {"marker": "benchmark", "data": "x" * 980}
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) < 1024:
        payload["pad"] = "y" * (1024 - len(encoded) - 20)
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    json_path.write_bytes(encoded[:1024].ljust(1024, b"x"))


if __name__ == "__main__":
    main()
