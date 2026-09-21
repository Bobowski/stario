#!/usr/bin/env python3
"""Aggregate repeated wrk samples with warmup discard and IQR outlier trimming."""

from __future__ import annotations

import json
import math
import sys


def _quartile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    weight = pos - lo
    return sorted_vals[lo] * (1 - weight) + sorted_vals[hi] * weight


def aggregate(values: list[float]) -> dict[str, float | int]:
    if not values:
        raise ValueError("no samples")

    raw = sorted(values)
    q1 = _quartile(raw, 0.25)
    q3 = _quartile(raw, 0.75)
    iqr = q3 - q1
    if iqr > 0 and len(raw) >= 4:
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        kept = [v for v in raw if lower <= v <= upper]
        if kept:
            used = kept
        else:
            used = raw
    else:
        used = raw

    n = len(used)
    median = _quartile(used, 0.5)
    mean = sum(used) / n
    if n > 1:
        variance = sum((v - mean) ** 2 for v in used) / (n - 1)
        stdev = math.sqrt(variance)
    else:
        stdev = 0.0

    return {
        "median": median,
        "mean": mean,
        "stdev": stdev,
        "min": min(used),
        "max": max(used),
        "n_raw": len(raw),
        "n_used": n,
        "outliers_removed": len(raw) - len(used),
    }


def main() -> int:
    values = [float(line.strip()) for line in sys.stdin if line.strip()]
    result = aggregate(values)
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
