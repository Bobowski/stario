# Server benchmarks — 2026-08-29 (standard GET path)

Cloud agent host. `DURATION=10s` `RUNS=5` `WARMUP=1` `THREADS=2`
`CONNECTIONS=128`, IQR trim. One worker. Cython only vs `cython-core`
`de72935` in `/tmp/stario-nosync`. This branch after dropping
cross-request caches: `733a25f`.

## What we keep (this request, not last request)

- Skip upload/Event state when there is no body (`mark_nobody`).
- `respond()` writes interned status / Date / type / length / body
  pieces via `writelines`. No join, no retained header blob.
- Arm idle timeout as “deadline 0”; the Date-tick sweeper fills
  `now + keep_alive`. No `loop.time()` on the wrk GET path.
- Skip `on_handler_done` when the eager task already completed a
  response under the NoOp tracer.
- `find_handler` then `create_task(handler(c, w))` every request.

## What we removed

Cross-request reuse on the same connection:

- One-slot `responses.text` UTF-8 cache
- Cached `respond()` header block while Date/status/type/length matched
- One-entry `(path, method)` handler slot on the connection
- `Router.routes_version` (only existed to invalidate that slot)

Those numbers (~1.20× vs `cython-core`, 1,284 ns in-process) included
the caches and are not comparable to this revision.

## In-process keep-alive `GET /plaintext`

Real protocol, no socket (`/tmp/profile_protocol.py`, 50k):

| Revision | ns/req | in-process req/s |
| --- | ---: | ---: |
| Before hot-path work | 1,717 | ~582k |
| With cross-request caches | 1,284 | ~779k |
| This revision (no caches) | 1,412 | ~708k |

## Consecutive pairs (baseline → branch)

### Pair 1 — `20260829T142759Z` → `20260829T143101Z`

| Endpoint | cython-core | this branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 139,849 ± 9,757 | 150,513 ± 272 | **1.08×** |
| JSON | 131,026 ± 420 | 154,145 ± 8,502 | **1.18×** |
| Params | 128,502 ± 327 | 141,964 ± 7,402 | **1.10×** |

### Pair 2 — `20260829T143408Z` → `20260829T143709Z`

| Endpoint | cython-core | this branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 147,136 ± 6,922 | 153,477 ± 104 | **1.04×** |
| JSON | 131,944 ± 4,523 | 142,540 ± 1,074 | **1.08×** |
| Params | 132,390 ± 2,903 | 136,814 ± 472 | **1.03×** |

Mean of the two ratios: plaintext **1.06×**, JSON **1.13×**, params **1.07×**.
Host GET noise is still several k req/s; treat the mean as directional.

758 tests passed.
