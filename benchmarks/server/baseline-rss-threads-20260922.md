# RAM and threads under a wrk spike (2026-09-22)

Same apps as the 1/2/4 suite. One 8-second wrk pass per case, not the
5-sample median. Use the req/s here only to see what the process was
doing while RSS was sampled. The throughput tables stay in
[`baseline-threads-vs-granian-20260921.md`](baseline-threads-vs-granian-20260921.md).

Sampler: every 200ms, sum `VmRSS` and `Pss` (`smaps_rollup`) over the
server pid and its children, and count `Threads`. PSS is the fair
column. RSS adds up shared libraries once per process, so Granian's
process workers look larger than the memory they actually own.

## Are the workers the same kind of thing?

Granian chooses at compile time (`granian/server/__init__.py`):

```python
Server = MPServer if BUILD_GIL else MTServer
```

Checked on the wheels this run used:

| Binary | `BUILD_GIL` | `sys._is_gil_enabled()` | What `--workers N` is |
| --- | --- | --- | --- |
| Granian 2.8.3 on 3.14t | `False` | `False` | N **threads** in one process (`MTServer`, `threading.Thread` named `granian-worker`) |
| Granian 2.8.3 on GIL 3.14 | `True` | `True` | N **processes** (`MPServer`, `multiprocessing`) |

The 3.14t process log says `free-threaded Python support is experimental`.
Under load that process had **one pid** at workers 1, 2, and 4. The GIL
build had a parent plus one pid per worker (3, 4, and 6 pids).

Stario `STARIO_THREADS=N` is also one process: the main thread is loop 0,
`stario-worker-1..N-1` are the others. On 3.14t that is the same shape as
Granian's free-threaded workers: one interpreter, N event loops, shared
Python objects. Granian's GIL workers are not that. They cannot share a
`Relay`.

Both sides were started with uvloop. Each loop created an `iou-sqp-*`
kernel thread. That is libuv's io_uring submission-queue poller (its
file threadpool when the kernel allows it), one per uvloop instance.
HTTP accept and read on that loop are still epoll. Granian's request
bytes are still read by Tokio, which is also epoll. Neither HTTP path
is an io_uring server.

## Plaintext spike

wrk `-t2 -c128 -d8s`. Peak during the run.

| Server | Loops | req/s | Peak RSS | Peak PSS | OS threads | Server CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Stario 3.14t | 1 | 106,079 | 40.9 MiB | 37.0 MiB | 6 | 1.0 core |
| Stario 3.14t | 2 | 158,511 | 42.3 MiB | 38.3 MiB | 9 | 2.0 cores |
| Stario 3.14t | 4 | 213,938 | 43.8 MiB | 39.9 MiB | 13 | 2.4 cores |
| Granian 3.14t threads | 1 | 118,168 | 53.0 MiB | 49.2 MiB | 7 | 2.5 cores |
| Granian 3.14t threads | 2 | 153,629 | 60.1 MiB | 56.3 MiB | 13 | 2.5 cores |
| Granian 3.14t threads | 4 | 151,905 | 66.1 MiB | 62.2 MiB | 25 | 2.6 cores |
| Granian GIL processes | 1 | 105,533 | 82.9 MiB | 57.0 MiB | 8 | 1.7 cores |
| Granian GIL processes | 2 | 166,798 | 118.1 MiB | 73.5 MiB | 14 | 2.4 cores |
| Granian GIL processes | 4 | 161,011 | 186.4 MiB | 104.3 MiB | 26 | 2.6 cores |

Stario's four loops add about **3 MiB** of PSS over one loop. Granian's
four free-threaded threads add about **13 MiB** over its one thread, and
sit ~22 MiB above Stario at the same N. Four GIL processes cost **104 MiB**
PSS, about 2.6× Stario's four threads, for less plaintext throughput.

Granian's "1 worker" is not one core. The Rust runtime, the Python
loop, and the uvloop `iou-sqp` thread were runnable together: 2.5 cores
to serve 118k req/s. Stario's one loop used 1.0 core for 106k.

## 64KB POST spike

wrk `-t2 -c32 -d8s` against `/ingest/64k`. This is the case that holds
body buffers.

| Server | Loops | req/s | Peak RSS | Peak PSS | OS threads |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stario 3.14t | 1 | 29,751 | 44.1 MiB | 40.2 MiB | 6 |
| Stario 3.14t | 2 | 68,003 | 47.2 MiB | 43.3 MiB | 9 |
| Stario 3.14t | 4 | 86,613 | 49.8 MiB | 45.9 MiB | 13 |
| Granian 3.14t threads | 1 | 49,334 | 69.3 MiB | 65.5 MiB | 7 |
| Granian 3.14t threads | 2 | 57,229 | 92.5 MiB | 88.7 MiB | 13 |
| Granian 3.14t threads | 4 | 57,385 | 92.6 MiB | 88.8 MiB | 25 |
| Granian GIL processes | 1 | 24,693 | 91.4 MiB | 65.5 MiB | 8 |
| Granian GIL processes | 2 | 47,128 | 126.6 MiB | 81.3 MiB | 14 |
| Granian GIL processes | 4 | 52,682 | 197.1 MiB | 113.5 MiB | 26 |

Under a 64KB body, Stario's four loops stay under **50 MiB** RSS.
Granian's free-threaded threads go to **93 MiB**. The GIL processes go
to **197 MiB** RSS / **114 MiB** PSS. Stario's PSS grows about 6 MiB
from the plaintext spike to this one. Granian's free-threaded PSS grows
about 27 MiB. The extra is body buffers in the Rust runtime, per worker,
on top of the Python heap.

## Relay across those threads

`Relay` is already the cross-thread bus. It does not need a second
design to be safe on `STARIO_THREADS`.

- `publish` takes `Relay.lock` only long enough to snapshot subscribers,
  then delivers outside the lock.
- Same-loop subscribers get the message inline. Other loops get
  `loop.call_soon_threadsafe`, and `deliver` re-checks `active` and
  `generation` under that subscription's `_inbox_lock`.
- A subscription is consumed only on the loop that entered it.
- Tests cover a worker thread publishing in, and two loops receiving
  the same subject (`tests/test_relay.py`).

That only works because the workers are threads in one process. Granian's
GIL `--workers` are separate interpreters; a `Relay` there would not
see the other workers' subscribers. Their free-threaded threads could
share one `Relay` the same way Stario does. They do not ship one.

What is still easy to get wrong, and is not a missing lock inside `Relay`:

- **The inbox is unbounded.** A handler on one loop that cannot keep up
  with `publish` from the other loops grows a `deque` for every message.
  N loops make that easier to hit. A bound, or dropping with a counter,
  is the product decision. Silently dropping would change the current
  contract.
- **The payload is shared.** `publish` hands the same object to every
  subscriber. A handler that mutates a dict races the other loops.
  Publish immutable values, or copy before publish.
- **App objects are not covered.** `Game` in the tiles example still
  needs its own lock around mutation and snapshot. `Relay` wakes the
  other loops; it does not make the dict thread-safe.
- **`_matching_patterns` is one process-wide `lru_cache`.** Correct
  under 3.14t, and it is a single lock on the publish path when the
  subject misses. Hot subjects hit the cache. Not the thing that will
  show up next to the plaintext bench.

No further lock belongs on the HTTP path for this. The shared object
the threads are for is this registry, and it already hops loops without
holding `Relay.lock` across `set_result`.
