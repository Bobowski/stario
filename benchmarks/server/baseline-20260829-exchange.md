# Server benchmarks — 2026-08-29 (`find_handler` + task)

Cloud agent host. Same knobs as the sync-handler A/B: `DURATION=10s`
`RUNS=5` `WARMUP=1` `THREADS=2` `CONNECTIONS=128`, IQR trim. One worker.
Do not compare absolute req/s to lab `baseline-20260828`.

Baseline is `cython-core` `de72935` in `/tmp/stario-nosync`.
This branch: `cursor/sync-handlers-7ae2` after dropping `App.__call__` /
`on_error` and scheduling `find_handler` then `create_task(handler(c, w))`.

## Consecutive pair (baseline → branch)

`20260829T114929Z` → `20260829T115531Z`.

| Endpoint | Python before | Python now | Ratio | Cython before | Cython now | Ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 73,202 | 74,448 | 1.02× | 136,668 | 145,053 | 1.06× |
| JSON | 74,001 | 74,690 | 1.01× | 129,030 | 126,066 | 0.98× |
| Params | 70,771 | 73,822 | 1.04× | 118,644 | 122,171 | 1.03× |

Cython plaintext stdev was ±6–7k on both sides. Treat the +6% as the
top of the noise band, not a stable win.

## Other runs on this host (same knobs)

`cython-core` Cython plaintext also printed 131,941 and 129,935 in
earlier sessions. Branch Cython plaintext also printed 137,681. GET
on this VM moves several thousand req/s between back-to-back suites.

## Failed shape (do not ship)

Putting Cython through the shared Python `schedule_request` helper
(`20260829T112337Z` → `20260829T112943Z`) lost about 5–10% vs the
same-day baseline. Extra Python (kwargs, closure, wrapper) is more
expensive than `App.__call__` on the eager GET path.

The kept shape is: Cython `_start_exchange` calls `find_handler` and
`create_task(handler)` itself; finish lives in the existing done
callback.

## Read of the result

Removing the App envelope does **not** close the Granian GET gap
(Granian was ~146–151k on this host earlier today). The leftover cost
is not `async def`, and it is not mainly `App.__call__` / `on_error`
MRO either. Those were already cheap next to routing + serialize +
task construction.
