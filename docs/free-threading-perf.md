# Where 3.14t time actually goes

> **Status (5.0):** items 1 and 3 below no longer exist. The Cython URL
> cache and `_url_lock` are gone, and `find_handler` walks a compiled
> `CRouter` trie with no `lru_cache` or exact map in front, so neither is
> shared per-request state any more. The measurements predate that.

`SO_REUSEPORT` is the right listen strategy for Linux TCP. The 1/2/4
captures already show it: two loops are ~1.9× plaintext, and the older
acceptor-handoff runtime landed in the same req/s band with worse
variance ([`baseline-threads-reuseport-20260921.md`](../benchmarks/server/baseline-threads-reuseport-20260921.md),
[`baseline-threads-vs-granian-20260921.md`](../benchmarks/server/baseline-threads-vs-granian-20260921.md)).

wrk keeps connections alive (`-c128`). The kernel hashes each new
connection onto one listener; every later request on that socket stays
on that loop. Accept runs ~128 times per bench, not once per request.
At ~200k req/s the listen path is noise. A different accept design will
not move plaintext on this suite.

What will move it is per-request Python work, and a few locks that are
still process-global after the loops were split.

## Single thread

CPython’s own number for 3.14t is about **8%** slower than the GIL build
on pyperformance, x86-64 Linux
([free-threading HOWTO](https://docs.python.org/3/howto/free-threading-python.html)).
That is biased reference counting plus per-object locks, not a missing
socket option.

Stario’s plaintext path allocates more than pyperformance’s hot loops, so
the tax on this server can be larger than 8%. Do not treat the historical
~148k GIL plaintext in `baseline-20260921.md` as the gap: that capture is
a different commit. The number to subtract is a GIL vs 3.14t Stario run
on **this** tree, same wrk knobs. Until that exists, optimize allocations
that cost on both interpreters and cost extra on 3.14t (deferred
refcount, GC of short-lived objects).

One plaintext request currently does all of this on the Python side:

1. **URL cache under a process lock.** `_path_for_url` takes
   `threading.Lock` (`_url_lock` in `protocol.pyx`) on every cacheable
   URL, including the hit. One URL (`/plaintext`) always hits. The lock
   is uncontended at N=1 and still a Python lock acquire per request.
2. **New `Request`.** The class docstring says it: not pooled with the
   exchange. Method, path, headers, and a fresh object every dispatch.
3. **Shared `functools.lru_cache` lookup.** `Router._lookup` (1024
   entries) is one cache for the process. 3.14’s lru_cache takes a lock
   so it stays correct with the GIL off. A hit is still a locked Python
   call. `_exact` is a dict keyed by `(host, path, method)` — another
   per-object lock if the cache misses.
4. **`asyncio.Task` even when the handler never awaits.**
   `App.create_task(..., eager_start=True)` constructs a `Task`. `Task`
   copies a `contextvars.Context` on the way in. The plaintext bench
   handler is `async def` with no `await`, so the task is done before
   `create_task` returns and is not stored in `App.tasks`. The
   allocation and the context copy still happen. Shutdown tracking does
   not need a task that is already finished.
5. **A new tuple for `writelines`.** `RequestExchange.respond` builds
   `(_status_line, date, content-type, content-length, CRLF, body)` on
   every response. Status line and content-type are interned; the tuple
   is not. Date bytes change once a second.

The exchange free-list is already thread-local. `_thread_pool()` still
does `getattr(threading.local(), "pool", None)` on acquire and release.

### What to change first (N=1 and N>1)

These are the same edits. N>1 just stops paying them N times plus the
lock convoy.

| Change | Why it hits 3.14t |
| --- | --- |
| Thread-local URL cache, or a `PyMutex` only around the miss | Hit path is a hash + `memcmp` with no Python lock. Today every thread shares `_UC_*` and `_url_lock`. |
| Thread-local route cache in front of the frozen trie | `lru_cache` on the shared `Router` is one lock for every request on every loop. A frozen table can be read from a per-thread dict. |
| Call a coroutine inline when `eager_start` finishes it | Skip `Task` + `copy_context` for handlers that return without suspending. Keep `Task` when the coro awaits (uploads, SSE). |
| Pool `Request` next to the exchange | One less object + header tuple per keep-alive request. |
| One cached response buffer per static body, rebuilt when the date box changes | Plaintext becomes one `write` of bytes assembled once a second per loop, instead of an 8-piece `writelines` tuple. Dynamic handlers stay on today’s path. |
| C thread-local for the exchange pool | Drop `threading.local` getattr on the recycle path. |

The date-stamped static buffer is the plaintext-shaped win (TechEmpower
does this). JSON and 64KB care more about the Task, the Request, and
the two caches, because those run even when the body is dynamic.

Uploads are a separate gap. On this host at N=1, Granian 3.14t did
**62.6k** on 64KB vs Stario **32.9k**. That is body copies and buffer
churn, not accept. Profile `Request.body()` / the exchange arena before
touching the listen socket for that row.

## Multiple threads

Reuseport already did the part that had to be in the kernel: each loop
accepts, parses, and runs handlers on its own connections. These are
still shared, so they cap scaling after the loops exist:

| Shared object | Taken on |
| --- | --- |
| Compiled `CRouter` trie (read-only after bootstrap) | every request |
| `App` shutdown flag read | every completed response (`response_completed`) |

The request-fields row scales worse than plaintext (1.61× at two loops
vs 1.90×). The bench working set is **4096** distinct paths. The URL
cache holds **256** keys and the route LRU holds **1024**. One shared
cache thrashes. With reuseport, each loop only sees the connections
hashed to it, so a **per-thread** cache of the same size covers that
loop’s subset and stops the threads from evicting each other.

`Relay` stays shared on purpose. It is not on the plaintext path. Do
not put a process-wide lock around dispatch to “be safe”.

### Listen designs that are not faster here

| Design | What it actually changes |
| --- | --- |
| **SO_REUSEPORT (current)** | One listen socket per loop. Kernel hashes the 4-tuple. Keep-alive stays on that loop, which is the affinity the protocol needs (`HttpProtocol`, exchange, writer). |
| **One acceptor + `connect_accepted_socket`** | Already measured. Same req/s, noisier. The `call_soon_threadsafe` is once per **connection**. With keep-alive that is ~128 hops per run. It matters for connection-per-request load, and it costs a thread that only accepts. |
| **One listen fd, `EPOLLEXCLUSIVE` in every loop** | Linux 4.5 wakes one epoll, not all of them. Cloudflare’s measurement of this pattern is LIFO: the worker that last returned to the loop gets the next connection, so the busiest loop gets more. Balance is worse than reuseport. uvloop does not let us register that flag on a socket another loop owns. Sharing one `socket` object across asyncio loops is also undefined. |
| **`SO_ATTACH_REUSEPORT_CBPF` / eBPF `sk_reuseport`** | Same sockets as now, custom steering (CPU id, least connections, drain). Useful when the default hash skews, or to stop sending SYNs at a loop that is shutting down. Needs `CAP_BPF`. Not a req/s feature at 4 cores and 128 keep-alive connections. |
| **Pin workers + `SO_INCOMING_CPU`** | The “pinned multireactor” result in OS benchmarks: each thread sticks to the core the NIC queued the packet on. Helps on a real multi-queue NIC with the load generator on another machine. On this VM, wrk and the server share 4 vCPUs and there is no hardware RSS to pin to. |
| **io_uring multishot accept** | Fewer syscalls per accept. Accept is not the cost. It is a different event loop, not a flag on `create_server`. |
| **GIL processes (Granian `--workers`)** | Scales, and shares nothing. `Relay` would need another bus. That is the trade this design refused. |

Two reuseport limits are real, and neither showed up as a throughput
cap on the keep-alive suite:

- **Hash skew.** Few connections, or one client port, can pile onto one
  listener. 128 wrk connections spread; a benchmark with `-c4` would not.
  Short-lived connections (no keep-alive) are where reuseport earns its
  keep, because accept *is* per request and a single acceptor saturates.
- **Close drops queued handshakes.** A listener leaving the reuseport
  group used to discard SYNs already hashed to it. Linux ≥ 5.14 can
  migrate them (`net.ipv4.tcp_migrate_req`). This host is 6.12. That
  matters for restart, not for the wrk median.

Unix sockets on this kernel return `EOPNOTSUPP` for a second
`SO_REUSEPORT` bind, so those stay at one thread. `EPOLLEXCLUSIVE` on a
shared Unix listen fd is the portable-looking alternative and has the
LIFO balance problem above. It is not worth a second server
implementation until a Unix deployment is actually multi-core bound.

## Order to try

1. Same-commit GIL vs 3.14t Stario, plaintext + request + JSON, N=1.
   That is the tax. Everything else is on top of it.
2. Thread-local URL cache and thread-local route cache. Re-bench N=1
   (lock overhead) and N=2/4 (contention, and the 4096-path row).
3. Inline completion for coroutines that never suspend. Re-bench
   plaintext and JSON.
4. Pool `Request`. Re-bench request fields.
5. Static response buffer keyed by the date box. Re-bench plaintext only.
6. Leave the listen path on `SO_REUSEPORT`. Revisit eBPF or
   `EPOLLEXCLUSIVE` only if a profile shows accept or wakeup time, which
   this suite does not.
