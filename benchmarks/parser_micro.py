#!/usr/bin/env python3
"""Parser microbench: httptools, zttp, Cython llhttp, complete-message C.

Measures one complete request at a time (the wrk keep-alive shape). This is
not a server bench — it isolates parser + language-boundary cost.

    PYTHONPATH=src:. .venv/bin/python benchmarks/parser_micro.py

Environment:

    PARSER_BENCH_ITERATIONS=200000
    PARSER_BENCH_REPEATS=7
    PARSER_BENCH_JSON=/tmp/parser-micro.json
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stario_cython.parser_bench import (  # noqa: E402
    bench_h1,
    bench_llhttp,
    parse_h1_once,
)

ITERATIONS = int(os.environ.get("PARSER_BENCH_ITERATIONS", "200000"))
REPEATS = int(os.environ.get("PARSER_BENCH_REPEATS", "7"))
JSON_PATH = os.environ.get("PARSER_BENCH_JSON")

GET = (
    b"GET /plaintext HTTP/1.1\r\n"
    b"Host: 127.0.0.1\r\n"
    b"User-Agent: wrk\r\n"
    b"Accept: */*\r\n"
    b"\r\n"
)

POST = (
    b"POST /echo HTTP/1.1\r\n"
    b"Host: 127.0.0.1\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 23\r\n"
    b"\r\n"
    b'{"name":"Ada","age":42}'
)

CASES = (("simple GET", GET), ("POST + JSON body", POST))


class _HttpToolsSink:
    __slots__ = ()

    def on_message_begin(self) -> None:
        return None

    def on_url(self, url: bytes) -> None:
        return None

    def on_header(self, name: bytes, value: bytes) -> None:
        return None

    def on_headers_complete(self) -> None:
        return None

    def on_body(self, body: bytes) -> None:
        return None

    def on_message_complete(self) -> None:
        return None


def _bench_httptools(payload: bytes, repeats: int) -> None:
    from httptools import HttpRequestParser

    sink = _HttpToolsSink()
    parser = HttpRequestParser(sink)
    for _ in range(repeats):
        parser.feed_data(payload)


def _bench_zttp(payload: bytes, repeats: int) -> None:
    import zttp

    conn = zttp.Connection(zttp.SERVER)
    need = zttp.NEED_DATA
    for _ in range(repeats):
        event = conn.receive_event(payload)
        if event is need:
            raise RuntimeError("zttp.receive_event returned NEED_DATA")
        while True:
            nxt = conn.next_event()
            if nxt is need:
                break
        conn.start_next_cycle()


def _time(fn, payload: bytes) -> float:
    start = time.perf_counter()
    fn(payload, ITERATIONS)
    return time.perf_counter() - start


def _median_rps(samples: list[float]) -> tuple[float, float]:
    rates = [ITERATIONS / s for s in samples]
    if len(rates) == 1:
        return rates[0], 0.0
    return statistics.median(rates), statistics.stdev(rates)


def _try_import(name: str):
    try:
        return __import__(name)
    except ImportError:
        return None


def main() -> int:
    consumed, method, flags = parse_h1_once(GET)
    if consumed != len(GET) or method != 1:
        print(
            f"stario_h1 sanity failed: consumed={consumed} method={method}",
            file=sys.stderr,
        )
        return 1
    print(f"stario_h1 GET ok consumed={consumed} method={method} flags={flags}")

    engines: list[tuple[str, object]] = [
        ("cython-h1 (complete C)", bench_h1),
        ("cython-llhttp (callbacks)", bench_llhttp),
    ]
    if _try_import("httptools") is not None:
        engines.append(("httptools (Python callbacks)", _bench_httptools))
    if _try_import("zttp") is not None:
        engines.append(("zttp (Zig / pull events)", _bench_zttp))
    else:
        print("zttp not installed; skip that row (uv pip install zttp)")

    rows: list[dict[str, object]] = []
    print(f"iterations={ITERATIONS} repeats={REPEATS}")
    print()
    for case_name, payload in CASES:
        print(f"## {case_name} ({len(payload)} bytes)")
        print(f"{'engine':<32} {'req/s':>12} {'stdev':>10}")
        print("-" * 56)
        for engine_name, fn in engines:
            fn(payload, max(200, ITERATIONS // 50))
            samples = [_time(fn, payload) for _ in range(REPEATS)]
            median, stdev = _median_rps(samples)
            print(f"{engine_name:<32} {median:12,.0f} {stdev:10,.0f}")
            rows.append(
                {
                    "case": case_name,
                    "engine": engine_name,
                    "bytes": len(payload),
                    "median_rps": median,
                    "stdev_rps": stdev,
                    "iterations": ITERATIONS,
                    "repeats": REPEATS,
                }
            )
        print()

    if JSON_PATH:
        Path(JSON_PATH).write_text(json.dumps(rows, indent=2) + "\n")
        print(f"wrote {JSON_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
