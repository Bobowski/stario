#!/usr/bin/env python3
"""Time production ParsedQuery.get / getlist (C-span index).

Pooled ``ParsedQuery``: ``__init__(raw)`` each request, then get/getlist.

Run:

    PYTHONPATH=src:. .venv/bin/python benchmarks/query_micro.py

Environment:

    QUERY_BENCH_ITERATIONS=80000
    QUERY_BENCH_REPEATS=7
    QUERY_BENCH_JSON=/tmp/query-micro.json
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pyximport

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BENCHMARKS))

import stario as _stario  # noqa: E402,F401

pyximport.install(
    build_dir=str(Path(tempfile.gettempdir()) / "stario-query-pyxbuild"),
    language_level=3,
)

from _query_micro import run_getlist, run_many_gets, run_one_get  # noqa: E402
from stario_cython.exchange import ParsedQuery  # noqa: E402

ITERATIONS = int(os.environ.get("QUERY_BENCH_ITERATIONS", "80000"))
REPEATS = int(os.environ.get("QUERY_BENCH_REPEATS", "7"))
JSON_PATH = os.environ.get("QUERY_BENCH_JSON")

PAIR_COUNTS = (1, 2, 3, 5, 8, 12, 16, 32, 48)


@dataclass(frozen=True, slots=True)
class Workload:
    name: str
    kind: str
    key_at: str = "first"
    reads: int = 1
    encoded: bool = False


WORKLOADS = (
    Workload("1 get first key", "get", "first", 1),
    Workload("1 get last key", "get", "last", 1),
    Workload("10 gets spread", "gets", "spread", 10),
    Workload("getlist one repeated key", "getlist", "first", 1),
)


def make_raw(
    count: int, *, encoded: bool, key_at: str, repeat_first: bool = False
) -> tuple[bytes, list[str]]:
    items: list[tuple[str, str]] = []
    for i in range(count):
        name = f"k{i:02d}"
        if encoded:
            items.append((name, f"hello world {i} %"))
        else:
            items.append((name, f"v{i:02d}"))
    if repeat_first and items:
        name, value = items[0]
        items.append((name, value + "b"))
    raw = urlencode(items, doseq=True).encode("ascii")
    names = [name for name, _ in items]
    return raw, names


def keys_for(names: list[str], workload: Workload) -> list[str]:
    if workload.kind == "get" and workload.key_at == "last":
        return [names[-1]]
    if workload.kind == "get":
        return [names[0]]
    if workload.kind == "getlist":
        return [names[0]]
    if workload.reads >= len(names):
        return list(names)
    if workload.reads <= 1:
        return [names[0]]
    return [names[round(i * (len(names) - 1) / (workload.reads - 1))] for i in range(workload.reads)]


def expected_acc(raw: bytes, keys: list[str], kind: str) -> int:
    q = ParsedQuery(raw)
    if kind == "getlist":
        return len(q.getlist(keys[0]))
    acc = 0
    for key in keys:
        got = q.get(key)
        if got is not None:
            acc += len(got)
    return acc


def run_case(raw: bytes, keys: list[str], kind: str, iterations: int) -> int:
    if kind == "getlist":
        return run_getlist(raw, keys[0], iterations)
    if kind == "get" and len(keys) == 1:
        return run_one_get(raw, keys[0], iterations)
    return run_many_gets(raw, keys, iterations)


def benchmark_one(raw: bytes, keys: list[str], kind: str) -> float:
    expected = expected_acc(raw, keys, kind)
    assert run_case(raw, keys, kind, 1) == expected
    warmup = max(1, ITERATIONS // 10)
    run_case(raw, keys, kind, warmup)
    samples: list[float] = []
    for _ in range(REPEATS):
        started = time.perf_counter_ns()
        checksum = run_case(raw, keys, kind, ITERATIONS)
        samples.append((time.perf_counter_ns() - started) / ITERATIONS)
        assert checksum == expected * ITERATIONS
    return statistics.median(samples)


def main() -> int:
    if ITERATIONS < 1 or REPEATS < 1:
        raise SystemExit("iterations and repeats must be positive")

    print(
        f"Production ParsedQuery.get: {ITERATIONS:,} requests × {REPEATS} repeats"
    )
    print("Lower is better; median nanoseconds per request.\n")

    rows: list[dict[str, object]] = []
    for workload in WORKLOADS:
        print(f"| pairs | {workload.name} |")
        print("|---:|---:|")
        for count in PAIR_COUNTS:
            if workload.reads > count > 0:
                continue
            raw, names = make_raw(
                count,
                encoded=workload.encoded,
                key_at=workload.key_at,
                repeat_first=workload.kind == "getlist",
            )
            keys = keys_for(names, workload)
            ns = benchmark_one(raw, keys, workload.kind)
            rows.append(
                {
                    "pairs": count,
                    "workload": workload.name,
                    "keys_read": keys,
                    "ns": ns,
                }
            )
            print(f"| {count} | {ns:.0f} ns |")
        print()

    if JSON_PATH:
        path = Path(JSON_PATH)
        path.write_text(
            json.dumps(
                {"iterations": ITERATIONS, "repeats": REPEATS, "rows": rows},
                indent=2,
            )
            + "\n"
        )
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
