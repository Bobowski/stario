# Stario event-loop comparison

Same Stario checkout (`stario` 4.1.x from this tree) and the same HTTP cases as
`benchmarks/server/` (`/plaintext`, `/json`, `/user/42`, `POST /validate`).
The only variable is the asyncio loop under `STARIO_LOOP`.

Loops this harness tries:

| Name | Implementation | Notes |
| --- | --- | --- |
| `asyncio` | CPython stdlib | Default; `SelectorEventLoop` on Linux |
| `uvloop` | [MagicStack/uvloop](https://github.com/MagicStack/uvloop) | libuv / Cython; the usual production extra |
| `zuvloop` | [Kludex/zuvloop](https://github.com/Kludex/zuvloop) | libuv / Zig; Python 3.14+ |
| `rloop` | [gi0baro/rloop](https://github.com/gi0baro/rloop) | Rust / mio; experimental, Unix |
| `rsloop` | [RustedBytes/rsloop](https://github.com/RustedBytes/rsloop) | Rust / io_uring on Linux |
| `uringcore` | [ankitkpandey1/uringcore](https://github.com/ankitkpandey1/uringcore) | Linux io_uring |

`winloop` is accepted by `STARIO_LOOP` but skipped here (Windows-only). A loop
that does not install or fails to serve is recorded as skipped/failed; the rest
of the suite still runs.

```bash
benchmarks/loops/run.sh
DURATION=30s RUNS=5 WARMUP=1 THREADS=2 CONNECTIONS=128 benchmarks/loops/run.sh
benchmarks/loops/run.sh asyncio uvloop zuvloop
```
