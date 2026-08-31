#!/usr/bin/env python3
"""Query get: C-span index vs previous scan-on-get vs eager parse-all.

Pooled ``ParsedQuery``: ``__init__(raw)`` each request, then get/getlist.

- index: production ``get`` (memcpy + C spans, memcmp names)
- scan: previous production path (``_get_scan``: walk ``&`` / ``=`` each get)
- eager: ``_get_eager`` (decode every name and value to Python lists)

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

from _query_micro import (  # noqa: E402
    MODE_EAGER,
    MODE_INDEX,
    MODE_SCAN,
    run_getlist,
    run_many_gets,
    run_one_get,
)
from stario_cython.exchange import ParsedQuery  # noqa: E402

ITERATIONS = int(os.environ.get("QUERY_BENCH_ITERATIONS", "80000"))
REPEATS = int(os.environ.get("QUERY_BENCH_REPEATS", "7"))
JSON_PATH = os.environ.get("QUERY_BENCH_JSON")

PAIR_COUNTS = (1, 2, 3, 4, 5, 8, 12, 16, 24, 32, 48)
MODES = (("index", MODE_INDEX), ("scan", MODE_SCAN), ("eager", MODE_EAGER))


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
    scan = ParsedQuery(raw)
    eager = ParsedQuery(raw)
    acc = 0
    if kind == "getlist":
        i = indexed.getlist(keys[0])
        s = scan._getlist_scan(keys[0])
        e = eager._getlist_eager(keys[0])
        assert i == s == e, (i, s, e)
        return len(i)
    for key in keys:
        i = indexed.get(key)
        s = scan._get_scan(key)
        e = eager._get_eager(key)
        assert i == s == e, (key, i, s, e)
        if i is not None:
            acc += len(i)
    return acc


def run_case(raw: bytes, keys: list[str], kind: str, mode: int, iterations: int) -> int:
    if kind == "getlist":
        return run_getlist(raw, keys[0], iterations, mode)
    if kind == "get" and len(keys) == 1:
        return run_one_get(raw, keys[0], iterations, mode)
    return run_many_gets(raw, keys, iterations, mode)


def benchmark_one(raw: bytes, keys: list[str], kind: str) -> dict[str, float]:
    expected = verify(raw, keys, kind)
    for _name, mode in MODES:
        assert run_case(raw, keys, kind, mode, 1) == expected

    warmup = max(1, ITERATIONS // 10)
    for _name, mode in MODES:
        run_case(raw, keys, kind, mode, warmup)

    samples: dict[str, list[float]] = {name: [] for name, _mode in MODES}
    for _ in range(REPEATS):
        for name, mode in MODES:
            started = time.perf_counter_ns()
            checksum = run_case(raw, keys, kind, mode, ITERATIONS)
            samples[name].append((time.perf_counter_ns() - started) / ITERATIONS)
            assert checksum == expected * ITERATIONS

    return {name: statistics.median(vals) for name, vals in samples.items()}


def spread_keys(names: list[str], reads: int) -> list[str]:
    """``reads`` distinct names, spaced through the query (not the same key)."""
    if reads <= 0:
        return []
    if reads >= len(names):
        return list(names)
    if reads == 1:
        return [names[0]]
    return [names[round(i * (len(names) - 1) / (reads - 1))] for i in range(reads)]


def fmt_ns(ns: float) -> str:
    return f"{ns:.0f}"


def run_matrix() -> list[dict[str, object]]:
    """Distinct keys read (K) vs pairs on the wire (N)."""
    pair_counts = (8, 16, 24, 32, 48)
    read_counts = (1, 2, 3, 4, 6, 8, 10, 12, 16)
    print(
        "Distinct params read (K) on a query with N pairs. "
        "Keys are spaced through the string.\n"
    )
    print("index/scan  (`i` = index ≤ scan × 1.05)\n")
    print("| N pairs \\ K reads | " + " | ".join(str(k) for k in read_counts) + " |")
    print("|---:|" + "---:|" * len(read_counts))
    rows: list[dict[str, object]] = []
    vs_scan_cells: dict[int, list[str]] = {}
    vs_eager_cells: dict[int, list[str]] = {}
    for count in pair_counts:
        raw, names = make_raw(count, encoded=False, key_at="spread")
        vs_scan_cells[count] = []
        vs_eager_cells[count] = []
        for reads in read_counts:
            if reads > count:
                vs_scan_cells[count].append("—")
                vs_eager_cells[count].append("—")
                continue
            keys = spread_keys(names, reads)
            times = benchmark_one(raw, keys, "gets")
            vs_scan = times["index"] / times["scan"] if times["scan"] else 0.0
            vs_eager = times["index"] / times["eager"] if times["eager"] else 0.0
            rows.append(
                {
                    "pairs": count,
                    "distinct_reads": reads,
                    **times,
                    "index_over_scan": vs_scan,
                    "index_over_eager": vs_eager,
                }
            )
            mark_s = "i" if vs_scan <= 1.05 else "s"
            mark_e = "i" if vs_eager <= 1.05 else "e"
            vs_scan_cells[count].append(f"{vs_scan:.2f}{mark_s}")
            vs_eager_cells[count].append(f"{vs_eager:.2f}{mark_e}")
        print(f"| {count} | " + " | ".join(vs_scan_cells[count]) + " |")
    print()
    print("index/eager  (`i` = index ≤ eager × 1.05)\n")
    print("| N pairs \\ K reads | " + " | ".join(str(k) for k in read_counts) + " |")
    print("|---:|" + "---:|" * len(read_counts))
    for count in pair_counts:
        print(f"| {count} | " + " | ".join(vs_eager_cells[count]) + " |")
    print()
    return rows


def main() -> int:
    if ITERATIONS < 1 or REPEATS < 1:
        raise SystemExit("iterations and repeats must be positive")

    print(
        f"Query index vs scan vs eager: {ITERATIONS:,} requests × {REPEATS} repeats"
    )
    print("Lower is better; median nanoseconds per request.\n")

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
            f"| pairs | {workload.name} index | scan | eager | idx/scan | idx/eager |"
        )
        print("|---:|---:|---:|---:|---:|---:|")
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
            times = benchmark_one(raw, keys, workload.kind)
            vs_scan = times["index"] / times["scan"] if times["scan"] else 0.0
            vs_eager = times["index"] / times["eager"] if times["eager"] else 0.0
            rows.append(
                {
                    "pairs": count,
                    "workload": workload.name,
                    "keys_read": keys,
                    **times,
                    "index_over_scan": vs_scan,
                    "index_over_eager": vs_eager,
                }
            )
            print(
                f"| {count} | {fmt_ns(times['index'])} | {fmt_ns(times['scan'])} | "
                f"{fmt_ns(times['eager'])} | {vs_scan:.2f}× | {vs_eager:.2f}× |"
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

    print("Where index sits (median over pair counts per workload):")
    by_work: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_work.setdefault(str(row["workload"]), []).append(row)
    for name, items in by_work.items():
        vs_scan = statistics.median(float(item["index_over_scan"]) for item in items)
        vs_eager = statistics.median(float(item["index_over_eager"]) for item in items)
        at = items[-1]
        print(
            f"- {name}: {vs_scan:.2f}× scan, {vs_eager:.2f}× eager "
            f"(at {at['pairs']} pairs: {fmt_ns(float(at['index']))} / "
            f"{fmt_ns(float(at['scan']))} / {fmt_ns(float(at['eager']))} ns)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
