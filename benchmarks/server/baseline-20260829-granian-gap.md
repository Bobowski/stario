# What would beat Granian on GET `/plaintext`

Profiler notes on commit `9e428a3` plus follow-up captures on
`cursor/cython-app-router-poc-b193`. Official suite numbers stay in
[`baseline-20260828-app-router.md`](baseline-20260828-app-router.md).

The remaining Granian lead on tiny GET is **not I/O and not the router**.
Stario already does **one `read` + one `writev` per request**. Granian pays
extra `write` / `futex` / `epoll` for a Rust→Python thread hop and still
wins because each Stario request spends ~700–900 ns more in Python/Cython
userspace: `asyncio.Task` + the App coroutine + the handler coroutine.

## The gap

Official sequential suite (`20260828T210831Z`, `RUNS=5`, `DURATION=10s`):

| | req/s | ns/req |
| --- | ---: | ---: |
| Stario Cython App | 136,005 | ~7,350 |
| Granian RSGI | 151,270 | ~6,610 |
| **Gap** | **~11%** | **~740 ns** |

Cython App/Router already closed about a third of the previous plaintext
delta (Python App 129.5k → 136.0k). Stario **leads** Granian on validate,
form, and every upload/body route in that run. This note is only about
jumping GET `/plaintext` / `/json` / `/user/{id}`.

## How Granian spends a request

Granian 2.8.2 RSGI, `--workers 1 --runtime-threads 1 --loop uvloop`:

1. **Rust thread** parses HTTP (httparse/hyper) and writes the response.
2. It wakes **uvloop** with `call_soon_threadsafe` / `uv_async_send`
   (`granian/_futures.py` `_cbsched_schedule`).
3. uvloop runs `future_watcher` → one `async def app(scope, proto)` with
   `if path == "/plaintext"` → `proto.response_str(...)`.
4. No framework App/Router. No `asyncio.Task` per request — a stub
   `_CBSchedulerTask` plus Rust `CallbackScheduler._run`.

py-spy on the spawn worker (need `--idle` and the `spawn_main` child, not
the resource tracker): uvloop thread ~44% `epoll_pwait`, ~20%
`future_watcher` / app; Rust thread ~46% `syscall` plus `sched_yield` /
`call_soon_threadsafe`.

## How Stario spends a request

Keep-alive GET `/plaintext` finishes **on the `data_received` stack**
(`eager_start=True`). Then uvloop `writev`s the 8-buffer `writelines`
tuple. py-spy native (no GIL filter, 12 s wrk, ~137k req/s):

| Unique samples | What |
| --- | --- |
| **51.5% `writev`** | one syscall per response (`uv__try_write`) |
| **19.3% `read`** | one syscall per request |
| **3.0% `epoll_pwait`** | idle/wait |
| **23.5% `data_received`** | parse + dispatch + App + handler + `respond` |

`writev` ∩ `data_received` = 0%. Two phases: handle on read, send later.

Under `data_received` (~23.5% of wall ≈ **~1.7 µs**, matches the FakeTransport
keepalive microbench):

| Frame / file | Unique % of wall | Notes |
| --- | ---: | --- |
| `create_task` | 19.0% | entire Task+App+handler+respond |
| `plaintext` handler | 10.2% | includes `text` + `respond` |
| `responses.text` | 8.7% | Python helper |
| `respond` | 7.8% | Cython `RequestExchange.respond` |
| `writelines` | 4.7% | uvloop queue (not the syscall) |
| `Router_find_handler` | **1.5%** | done |

cProfile on the same keepalive path only sees Python: `responses.text` and
`str.encode`. Cython/Task/llhttp are invisible — do not use cProfile alone.

## Syscalls (strace, ratios still valid even though strace tanks absolute req/s)

Per request during wrk `-c128` GET `/plaintext`:

| | Stario | Granian worker |
| --- | ---: | ---: |
| `read` / `recvfrom` | **1.00** | **1.00** `recvfrom` |
| `writev` | **1.00** | **1.00** |
| extra `write` | 0 | **~1.07** (async wake / eventfd) |
| `futex` | 0 | **~0.86** |
| `epoll_pwait` | ~0.008 | **~0.13** |
| `sched_yield` | 0 | **~0.18** |

Stario is the cheaper I/O shape. Chasing `writev` coalescing or a Rust HTTP
core will not close a 740 ns userspace gap; Granian already does *more*
syscalls per GET and still wins.

## Microbench (uvloop, FakeTransport keepalive)

`PYTHONPATH=src:. .venv/bin/python benchmarks/server/profile_plaintext.py`

| Piece | ns |
| ---: | ---: |
| router static `/plaintext` | 57 |
| `str.encode("Hello, World!")` | 23 |
| empty coroutine alloc | 53 |
| `asyncio.Task` empty eager | **303** |
| `responses.text` DummyWriter | 396 |
| handler `send` + `find_handler` (no App) | 665 |
| App `send` DummyWriter (no Task) | 1002 |
| App+Task DummyWriter `text` | 1278 |
| protocol keepalive GET `/plaintext` | **1525–1730** |

Additive:

- **Task ≈ 280–300 ns** (1278 − 1002)
- **App `__call__` wrapper ≈ 340 ns** (1002 − 665) — try/except/finally,
  type checks, `await handler`
- **handler coroutine vs `text()` ≈ 270 ns** (665 − 396)
- **llhttp + real `respond` + recycle ≈ 250–410 ns** vs dummy

Task + App wrapper ≈ **620 ns**. That is most of the 740 ns Granian gap.
Dropping the inner `async def` as well (~270 ns) overshoots.

## What would jump ahead (ranked)

### 1. Do not allocate `asyncio.Task` on non-suspending GET (~300 ns)

`HttpProtocol._start_exchange` always does `asyncio.Task(app(...), eager_start=True)`
even when the handler never awaits. The Task is `.done()` before
`_start_exchange` returns; the object still costs ~300 ns.

**Do not** hand-roll `coro.send(None)` + a home-grown resume. A first
`send()` that yields must follow `asyncio.Task.__step` exactly
(`_asyncio_future_blocking`, bare `yield` from `asyncio.sleep(0)`,
`send(None)` not `send(result)`, `c.alive()` / cancel, pipelined recycle).
A partial clone 500s streaming POST, breaks `sleep(0)` pipeline tests, and
deadlocks `connection_lost` + `c.alive()`.

Cleaner shapes:

- Keep `asyncio.Task` as the suspend path. For the complete-immediately
  case, drive with the same stepper CPython uses, or
- Register **sync** handlers (`def plaintext(c, w)` / cdef) and call them
  from `_start_exchange` with no Task and no App coroutine.

Either path should land ~5–8k req/s on this host if the rest stays equal
(~141–144k). Still behind Granian alone.

### 2. Flatten App `__call__` off the hot path (~340 ns)

`App.__call__` is a coroutine: trailing-slash 308, `find_handler`,
`await handler`, “handler forgot to respond”, error handlers, `finally: end()`.
On GET `/plaintext` that is a second generator plus try/except for work that
is already a cdef lookup.

Move routing + `end()`/`abort()` into a **cdef protocol callback**. Keep
`async def __call__` for the Python httptools server and for handlers that
suspend. Combined with (1) this is roughly **match Granian** on plaintext
(~620 ns of the ~740 ns gap).

### 3. Sync (or cdef) handlers (~270 ns more → ahead)

Benchmark `async def plaintext` only exists so `await handler` has something
to await. `responses.text` is synchronous. Granian’s app is still `async def`
but it is **one** coroutine, not App + handler.

`def plaintext(c, w): responses.text(w, HELLO)` called from C skips the
inner generator. (1)+(2)+(3) is ~890 ns — enough to go **through** Granian
on this box if I/O stays 1×`writev`.

Fair vs handler policy: still encode `"Hello, World!"` every request; do not
pre-serialize the body.

### 4. Cheaper `respond` / `responses.text` (100–300 ns, second wave)

`responses.text` is still Python. `respond()` builds an 8-tuple of interned
buffers (`STATUS_200`, date box, `CT_PREFIX`, …) and `writelines`. Header
**templates** (not cached body bytes) are fair under
`benchmarks/README.md` handler policy. Joining the header block into 1–2
iovecs is optional; strace already shows a single `writev`. The win is fewer
Python tuple/object ops, not fewer syscalls.

Pre-encoded `HELLO_B` vs `text()` was only ~55 ns in the DummyWriter
microbench. Encode is not the gap.

### 5. Do not spend more on Router, registration, or `Request.host`

57 ns static match, 1.5% of wall. Host routing is unused on this bench.
`handle` / `use` is cold.

### 6. Do not rewrite HTTP in Rust to beat this GET

Granian’s Rust I/O is why they can afford a thread hop and a slower syscall
mix. Stario’s uvloop path is already 1 read + 1 writev. The GET deficit is
Python scheduling, not libuv.

A native HTTP core is a different product goal (HTTP/2, TLS, multi-thread).
It is not the 740 ns plaintext lever.

## Suggested order of work

1. **Sync/cdef dispatch on the protocol callback** for handlers that do not
   suspend: no Task, no App coroutine, no inner `async def`. Biggest jump,
   one design, avoids cloning `Task.__step`.
2. Keep `asyncio.Task(eager_start=True)` for `await req.body()` / streaming /
   `c.alive()`.
3. Optionally cdef `responses.text` or inline encode+`respond` for the
   helper.
4. Re-measure only GET `/plaintext`, `/json`, `/user/{id}` against Granian
   RSGI with the same wrk knobs as the official suite.

## Reproduce the profiles

```bash
# Isolate userspace pieces (needs tests/ on PYTHONPATH):
PYTHONPATH=src:. .venv/bin/python benchmarks/server/profile_plaintext.py

# Live wrk + py-spy (Stario + Granian). Granian needs --idle and the
# spawn_main worker PID; --nonblocking produced empty captures.
DURATION=12s PROFILE_PYTHON=1 benchmarks/server/profile_load.sh
```

Python-only py-spy (no `--native`) attributes almost everything to
`uvloop.run` / `responses.text` because App/protocol are compiled. Use
`--native` for Cython frames.
