# Server benchmarks — 2026-08-29 (Cython GET hot path)

Cloud agent host. `DURATION=10s` `RUNS=5` `WARMUP=1` `THREADS=2`
`CONNECTIONS=128`, IQR trim. One worker. Cython only vs `cython-core`
`de72935` in `/tmp/stario-nosync`. This branch: `8cbaf14` + follow-up
that keeps `asyncio.Task` (see below).

## What the profiler showed

In-process keep-alive `GET /plaintext` through the real protocol (no
socket): **1,717 ns/req** on the previous revision (~582k req/s). wrk
on the same host was ~140k. The leftover vs Granian was not exception
mapping or auto-`end()` — those never ran on this path.

Breakdown of the 1.7 µs:

- `asyncio.Task(..., eager_start=True)` for a handler that never awaits:
  ~320 ns. Trying to `send()` then `await` the same Future is invalid
  (`RuntimeError: await wasn't used with future`). Kept the Task.
- `reset_body` + `c_complete` on every GET (no upload): real work.
- `find_handler` Python call every request (LRU hit is cheap; the call
  from Cython is not).
- `respond()` rebuilt an 8-part header tuple every time. Date is stable
  for one second.
- `responses.text` encoded `HELLO` every request.
- `_arm_timeout` called `loop.time()` after every keep-alive response.

## What we changed

- Skip upload/Event state when there is no body (`mark_nobody`).
- One-entry `(path, method)` handler cache on the connection, invalidated
  by `Router.routes_version`.
- Cache the `respond()` header block while `Date` / status / type /
  length are unchanged.
- One-slot UTF-8 cache in `responses.text`.
- Arm idle timeout as “deadline 0” and let the Date-tick sweeper fill
  `now + keep_alive`. No `loop.time()` on the wrk path.
- Skip `on_handler_done` when the eager task already completed a
  response under the NoOp tracer.

In-process after: **1,284 ns/req** (~779k req/s).

## Consecutive pairs (baseline → branch)

### Pair 1 — `20260829T134800Z` → `20260829T135105Z`

| Endpoint | cython-core | this branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 139,286 ± 1,307 | 169,936 ± 8,943 | **1.22×** |
| JSON | 132,012 ± 758 | 149,954 ± 7,807 | **1.14×** |
| Params | 127,895 ± 140 | 152,777 ± 8,346 | **1.19×** |

### Pair 2 — `20260829T135412Z` → `20260829T135717Z`

| Endpoint | cython-core | this branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 141,570 ± 6,181 | 165,941 ± 7,662 | **1.17×** |
| JSON | 124,176 ± 313 | 150,817 ± 674 | **1.21×** |
| Params | 120,212 ± 3,609 | 145,353 ± 482 | **1.21×** |

Mean of the two ratios: plaintext **1.20×**, JSON **1.18×**, params **1.20×**.

Earlier the same day, Granian RSGI plaintext on this host was ~146k.
These Cython numbers sit above that. Do not treat it as a locked
Granian win until a same-host three-way rerun.

758 tests passed.
