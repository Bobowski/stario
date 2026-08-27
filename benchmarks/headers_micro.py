#!/usr/bin/env python3
"""Compare eager/lazy dict request headers with arena-backed lookup strategies.

The benchmark models a pooled exchange: C buffers and the Python dict survive
between requests, while logical contents are reset. Header names are
lowercased and hashed while entering the arena. Queries use already-normalized
bytes, matching the Cython protocol's internal ``unsafe_get`` path.

Run:

    PYTHONPATH=src:. .venv/bin/python benchmarks/headers_micro.py

Environment:

    HEADERS_BENCH_ITERATIONS=100000
    HEADERS_BENCH_REPEATS=7
    HEADERS_BENCH_JSON=/tmp/headers-micro.json
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

import pyximport

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BENCHMARKS))

pyximport.install(
    build_dir=str(Path(tempfile.gettempdir()) / "stario-headers-pyxbuild"),
    language_level=3,
)

from _headers_micro import (  # type: ignore[import-not-found]  # noqa: E402
    MODE_ADAPTIVE,
    MODE_ARENA_SCAN,
    MODE_EAGER_DICT,
    MODE_LAZY_DICT,
    HeaderStore,
)

ITERATIONS = int(os.environ.get("HEADERS_BENCH_ITERATIONS", "100000"))
REPEATS = int(os.environ.get("HEADERS_BENCH_REPEATS", "7"))
JSON_PATH = os.environ.get("HEADERS_BENCH_JSON")

STRATEGIES = {
    "eager-dict": MODE_EAGER_DICT,
    "lazy-dict": MODE_LAZY_DICT,
    "arena-scan": MODE_ARENA_SCAN,
    "adaptive-3": MODE_ADAPTIVE,
}

COMMON_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"Host", b"api.example.com"),
    (b"User-Agent", b"Mozilla/5.0 benchmark"),
    (b"Accept", b"application/json"),
    (b"Cookie", b"session=abc123; theme=dark"),
    (b"Authorization", b"Bearer benchmark-token"),
    (b"X-Request-ID", b"01J6BENCHMARKREQUEST"),
    (b"Accept-Encoding", b"gzip, br"),
    (b"Connection", b"keep-alive"),
    (b"Accept-Language", b"en-US,en;q=0.9"),
    (b"Cache-Control", b"no-cache"),
    (b"Sec-Fetch-Site", b"same-origin"),
    (b"Sec-Fetch-Mode", b"cors"),
    (b"Sec-Fetch-Dest", b"empty"),
    (b"Origin", b"https://example.com"),
    (b"Referer", b"https://example.com/app"),
    (b"Content-Type", b"application/json"),
    (b"X-Forwarded-For", b"192.0.2.1"),
    (b"X-Forwarded-Proto", b"https"),
    (b"X-Real-IP", b"192.0.2.1"),
    (b"Traceparent", b"00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"),
    (b"Priority", b"u=1, i"),
    (b"DNT", b"1"),
    (b"Sec-GPC", b"1"),
    (b"TE", b"trailers"),
)


@dataclass(frozen=True, slots=True)
class Workload:
    name: str
    gets: tuple[bytes, ...] = ()
    getlists: tuple[bytes, ...] = ()
    repeated_cookies: bool = False


WORKLOADS = (
    Workload("no application reads"),
    Workload("one arbitrary read", gets=(b"authorization",)),
    Workload("one missing optional read", gets=(b"x-optional-feature",)),
    Workload(
        "three distinct reads",
        gets=(b"authorization", b"user-agent", b"x-request-id"),
    ),
    Workload("same header 8x", gets=(b"authorization",) * 8),
    Workload("same missing header 8x", gets=(b"x-optional-feature",) * 8),
    Workload("same header 64x", gets=(b"authorization",) * 64),
    Workload("single Cookie getlist", getlists=(b"cookie",)),
    Workload(
        "three Cookie lines getlist",
        getlists=(b"cookie",),
        repeated_cookies=True,
    ),
    Workload(
        "three Cookie lines getlist 3x",
        getlists=(b"cookie",) * 3,
        repeated_cookies=True,
    ),
)


def headers_for(count: int, *, repeated_cookies: bool) -> tuple[tuple[bytes, bytes], ...]:
    pairs = list(COMMON_HEADERS[:count])
    while len(pairs) < count:
        index = len(pairs)
        pairs.append((f"X-Padding-{index}".encode(), f"value-{index}".encode()))
    if repeated_cookies:
        cookie_index = next(
            i
            for i, (name, _) in enumerate(pairs)
            if name.lower() == b"cookie"
        )
        pairs[cookie_index : cookie_index + 1] = [
            (b"Cookie", b"session=abc123"),
            (b"cookie", b"theme=dark"),
            (b"COOKIE", b"experiment=B"),
        ]
    return tuple(pairs)


def expected_values(
    pairs: tuple[tuple[bytes, bytes], ...],
    name: bytes,
) -> list[bytes]:
    return [value for key, value in pairs if key.lower() == name]


def verify_backend(
    store: HeaderStore,
    pairs: tuple[tuple[bytes, bytes], ...],
    workload: Workload,
) -> int:
    store.load(pairs)
    checksum = 0
    for name in workload.gets:
        expected = expected_values(pairs, name)
        actual = store.get(name)
        assert actual == (expected[0] if expected else None)
        if actual is not None:
            checksum += len(actual)
    for name in workload.getlists:
        expected = expected_values(pairs, name)
        actual = store.getlist(name)
        assert actual == expected
        checksum += len(actual) + sum(map(len, actual))
    return checksum


def benchmark_one(
    mode: int,
    pairs: tuple[tuple[bytes, bytes], ...],
    workload: Workload,
) -> tuple[float, float]:
    store = HeaderStore(mode, promote_after=3)
    expected = verify_backend(store, pairs, workload)
    assert store.run_batch(pairs, workload.gets, workload.getlists, 1) == expected

    warmup = max(1, ITERATIONS // 10)
    store.run_batch(pairs, workload.gets, workload.getlists, warmup)

    samples: list[float] = []
    materializations = 0
    for _ in range(REPEATS):
        before = store.materializations
        started = time.perf_counter_ns()
        checksum = store.run_batch(
            pairs,
            workload.gets,
            workload.getlists,
            ITERATIONS,
        )
        elapsed = time.perf_counter_ns() - started
        assert checksum == expected * ITERATIONS
        samples.append(elapsed / ITERATIONS)
        materializations += store.materializations - before
    return statistics.median(samples), materializations / (ITERATIONS * REPEATS)


def main() -> int:
    if ITERATIONS < 1 or REPEATS < 1:
        raise SystemExit("iterations and repeats must be positive")

    print(
        f"Cython request-header microbenchmark: "
        f"{ITERATIONS:,} requests × {REPEATS} repeats"
    )
    print("Lower is better; values are median nanoseconds per request.\n")

    rows: list[dict[str, object]] = []
    for count in (8, 16, 32):
        for workload in WORKLOADS:
            pairs = headers_for(count, repeated_cookies=workload.repeated_cookies)
            timings: dict[str, float] = {}
            promotions: dict[str, float] = {}
            expected: int | None = None
            for strategy, mode in STRATEGIES.items():
                store = HeaderStore(mode, promote_after=3)
                checksum = verify_backend(store, pairs, workload)
                if expected is None:
                    expected = checksum
                else:
                    assert checksum == expected
                timing, promoted = benchmark_one(mode, pairs, workload)
                timings[strategy] = timing
                promotions[strategy] = promoted

            rows.append(
                {
                    "headers": len(pairs),
                    "workload": workload.name,
                    "nanoseconds_per_request": timings,
                    "materializations_per_request": promotions,
                }
            )

    print(
        "| fields | workload | eager dict | lazy dict | arena scan | "
        "adaptive-3 | arena vs lazy |"
    )
    print("|---:|---|---:|---:|---:|---:|---:|")
    for row in rows:
        timings = row["nanoseconds_per_request"]
        assert isinstance(timings, dict)
        lazy = timings["lazy-dict"]
        arena = timings["arena-scan"]
        print(
            f"| {row['headers']} | {row['workload']} | "
            f"{timings['eager-dict']:.0f} ns | "
            f"{lazy:.0f} ns | "
            f"{arena:.0f} ns | "
            f"{timings['adaptive-3']:.0f} ns | "
            f"{(arena / lazy - 1) * 100:+.1f}% |"
        )

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
        print(f"\nWrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
