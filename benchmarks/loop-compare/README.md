# Loop comparison: uvloop vs zuvloop

Compares **main** and **cython-core** on [uvloop](https://github.com/MagicStack/uvloop)
and [zuvloop](https://github.com/Kludex/zuvloop) across the official Stario HTTP
suite from `cython-core` (read + upload endpoints).

**Results from this machine:** [RESULTS.md](RESULTS.md).

```bash
# worktrees (cython-core required for the expanded app + Cython protocol)
git worktree add /tmp/stario-cython origin/cython-core

# official knobs (same as baseline-20260828)
DURATION=10s RUNS=5 WARMUP=1 THREADS=2 \
  CONNECTIONS=128 UPLOAD_CONNECTIONS=32 \
  benchmarks/loop-compare/run.sh
```

HTML compare/micro (loop-independent) and `headers_micro` (cython-core only):

```bash
benchmarks/loop-compare/run-html.sh
```
