#!/usr/bin/env python3
"""Indexed query get vs eager parse-all (same Cython decoder).

Pooled ``ParsedQuery``: ``__init__(raw)`` each request, then get/getlist.
Production ``get`` copies the query, fills a C span table, memcmps names.
Eager baseline calls ``_get_eager`` (decode every name and value to Python
lists, then list-scan).

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

PAIR_COUNTS = (1, 2, 3, 4, 5, 8, 12, 16, 24, 32, 48)


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
    Workload("1 get missing", "get", "missing", 1),
    Workload("2 gets first+last", "gets", "spread", 2),
    Workload("3 gets spread", "gets", "spread", 3),
    Workload("10 gets spread", "gets", "spread", 10),
    Workload("get every key", "gets", "all", 0),
    Workload("getlist one repeated key", "getlist", "first", 1),
    Workload("1 get first, encoded values", "get", "first", 1, encoded=True),
    Workload("1 get last, encoded values", "get", "last", 1, encoded=True),
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
    if key_at == "missing":
        return raw, names
    return raw, names


def keys_for(names: list[str], workload: Workload) -> list[str]:
    if workload.kind == "get" and workload.key_at == "missing":
        return ["missing"]
    if workload.kind == "get" and workload.key_at == "last":
        return [names[-1]]
    if workload.kind == "get":
        return [names[0]]
    if workload.kind == "getlist":
        return [names[0]]
    if workload.key_at == "all" or workload.reads <= 0:
        return list(names)
    if workload.reads == 2:
        if len(names) == 1:
            return [names[0], names[0]]
        return [names[0], names[-1]]
    if len(names) == 1:
        return [names[0]] * min(3, workload.reads)
    idxs = [0, len(names) // 2, len(names) - 1]
    picked: list[str] = []
    for idx in idxs:
        name = names[idx]
        if name not in picked:
            picked.append(name)
        if len(picked) >= workload.reads:
            break
    while len(picked) < min(workload.reads, len(names)):
        picked.append(names[len(picked)])
    return picked


def verify(raw: bytes, keys: list[str], kind: str) -> int:
    indexed = ParsedQuery(raw)
    eager = ParsedQuery(raw)
    acc = 0
    if kind == "getlist":
        s = indexed.getlist(keys[0])
        e = eager._getlist_eager(keys[0])
        assert s == e, (s, e)
        return len(s)
    for key in keys:
        s = indexed.get(key)
        e = eager._get_eager(key)
        assert s == e, (key, s, e)
        if s is not None:
            acc += len(s)
    return acc


def run_case(raw: bytes, keys: list[str], kind: str, eager: bool, iterations: int) -> int:
    if kind == "getlist":
        return run_getlist(raw, keys[0], iterations, eager)
    if kind == "get" and len(keys) == 1:
        return run_one_get(raw, keys[0], iterations, eager)
    return run_many_gets(raw, keys, iterations, eager)


def benchmark_one(raw: bytes, keys: list[str], kind: str) -> tuple[float, float]:
    expected_index = verify(raw, keys, kind)
    expected_eager = verify(raw, keys, kind)
    assert expected_index == expected_eager
    assert run_case(raw, keys, kind, False, 1) == expected_index
    assert run_case(raw, keys, kind, True, 1) == expected_eager

    warmup = max(1, ITERATIONS // 10)
    run_case(raw, keys, kind, False, warmup)
    run_case(raw, keys, kind, True, warmup)

    index_samples: list[float] = []
    eager_samples: list[float] = []
    for _ in range(REPEATS):
        started = time.perf_counter_ns()
        checksum = run_case(raw, keys, kind, False, ITERATIONS)
        index_samples.append((time.perf_counter_ns() - started) / ITERATIONS)
        assert checksum == expected_index * ITERATIONS

        started = time.perf_counter_ns()
        checksum = run_case(raw, keys, kind, True, ITERATIONS)
        eager_samples.append((time.perf_counter_ns() - started) / ITERATIONS)
        assert checksum == expected_eager * ITERATIONS

    return statistics.median(index_samples), statistics.median(eager_samples)


def spread_keys(names: list[str], reads: int) -> list[str]:
    """``reads`` distinct names, spaced through the query (not the same key)."""
    if reads <= 0:
        return []
    if reads >= len(names):
        return list(names)
    if reads == 1:
        return [names[0]]
    return [names[round(i * (len(names) - 1) / (reads - 1))] for i in range(reads)]


def run_matrix() -> list[dict[str, object]]:
    """Distinct keys read (K) vs pairs on the wire (N). Long queries included."""
    pair_counts = (8, 16, 24, 32, 48)
    read_counts = (1, 2, 3, 4, 6, 8, 12, 16, 24)
    print(
        "Distinct params read (K) on a query with N pairs. "
        "Keys are spaced through the string.\n"
    )
    print("| N pairs \\ K reads | " + " | ".join(str(k) for k in read_counts) + " |")
    print("|---:|" + "---:|" * len(read_counts))
    rows: list[dict[str, object]] = []
    for count in pair_counts:
        raw, names = make_raw(count, encoded=False, key_at="spread")
        cells: list[str] = []
        for reads in read_counts:
            if reads > count:
                cells.append("—")
                continue
            keys = spread_keys(names, reads)
            index_ns, eager_ns = benchmark_one(raw, keys, "gets")
            ratio = index_ns / eager_ns if eager_ns else 0.0
            rows.append(
                {
                    "pairs": count,
                    "distinct_reads": reads,
                    "index_ns": index_ns,
                    "eager_ns": eager_ns,
                    "ratio": ratio,
                }
            )
            mark = "i" if ratio <= 1.05 else "e"
            cells.append(f"{ratio:.2f}{mark}")
        print(f"| {count} | " + " | ".join(cells) + " |")
    print()
    print("Cell is index/eager. `i` = index wins or tie (≤1.05). `e` = eager ahead.\n")
    return rows


def main() -> int:
    if ITERATIONS < 1 or REPEATS < 1:
        raise SystemExit("iterations and repeats must be positive")

    print(
        f"Query index vs eager parse-all: {ITERATIONS:,} requests × {REPEATS} repeats"
    )
    print("Lower is better; median nanoseconds per request. Ratio < 1 means index wins.\n")
    print(
        "`1-3 named reads` means 1-3 distinct parameter names on one query, "
        "not the same name three times. Pair count is how long the query is.\n"
    )

    if "--matrix" in sys.argv or os.environ.get("QUERY_BENCH_MATRIX") == "1":
        print("Distinct-reads × pair-count matrix\n")
        matrix_rows = run_matrix()
        if JSON_PATH:
            path = Path(JSON_PATH)
            path.write_text(
                json.dumps(
                    {
                        "iterations": ITERATIONS,
                        "repeats": REPEATS,
                        "matrix": matrix_rows,
                    },
                    indent=2,
                )
                + "\n"
            )
            print(f"Wrote {path}")
        return 0

    rows: list[dict[str, object]] = []
    for workload in WORKLOADS:
        print(
            f"| pairs | {workload.name} index | eager | index/eager |"
        )
        print("|---:|---:|---:|---:|")
        for count in PAIR_COUNTS:
            if workload.reads > count > 0:
                continue
            if workload.key_at == "all" and count > 16:
                continue
            raw, names = make_raw(
                count,
                encoded=workload.encoded,
                key_at=workload.key_at,
                repeat_first=workload.kind == "getlist",
            )
            keys = keys_for(names, workload)
            index_ns, eager_ns = benchmark_one(raw, keys, workload.kind)
            ratio = index_ns / eager_ns if eager_ns else 0.0
            rows.append(
                {
                    "pairs": count,
                    "workload": workload.name,
                    "keys_read": keys,
                    "index_ns": index_ns,
                    "eager_ns": eager_ns,
                    "ratio": ratio,
                }
            )
            print(
                f"| {count} | {index_ns:.0f} ns | {eager_ns:.0f} ns | {ratio:.2f}× |"
            )
        print()

    if JSON_PATH:
        path = Path(JSON_PATH)
        path.write_text(
            json.dumps(
                {
                    "iterations": ITERATIONS,
                    "repeats": REPEATS,
                    "rows": rows,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"Wrote {path}")

    print("Crossover (index loses, ratio > 1.05) per workload:")
    by_work: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_work.setdefault(str(row["workload"]), []).append(row)
    for name, items in by_work.items():
        lose = next((item for item in items if float(item["ratio"]) > 1.05), None)
        if lose is None:
            print(f"- {name}: index ahead through {items[-1]['pairs']} pairs")
        else:
            print(
                f"- {name}: eager ahead from {lose['pairs']} pairs "
                f"({float(lose['ratio']):.2f}×)"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
