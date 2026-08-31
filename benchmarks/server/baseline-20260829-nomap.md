# Server benchmarks — 2026-08-29 (no exception mapping, Cython 308)

Cloud agent host. Same knobs as the earlier same-day A/Bs: `DURATION=10s`
`RUNS=5` `WARMUP=1` `THREADS=2` `CONNECTIONS=128`, IQR trim. One worker.
Cython only (`stario-cython`) against `cython-core` `de72935` in
`/tmp/stario-nosync`. This branch: `ae10c94`.

This revision stops mapping exceptions to HTTP, stops auto-`end()`, and
writes trailing-slash 308 from the raw request target in the Cython
protocol (no handler task). 404/405 stay `find_handler` results.

## Consecutive pairs (baseline → branch)

### Pair 1 — `20260829T131244Z` → `20260829T131549Z`

| Endpoint | cython-core | this branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 143,962 ± 1,269 | 141,615 ± 187 | 0.98× |
| JSON | 132,847 ± 190 | 131,410 ± 1,315 | 0.99× |
| Params | 129,700 ± 599 | 127,844 ± 119 | 0.99× |

### Pair 2 — `20260829T131900Z` → `20260829T132206Z`

| Endpoint | cython-core | this branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 140,256 ± 180 | 144,011 ± 3,132 | 1.03× |
| JSON | 136,182 ± 7,427 | 134,854 ± 356 | 0.99× |
| Params | 128,234 ± 364 | 130,946 ± 55 | 1.02× |

Mean of the two ratios: plaintext 1.00×, JSON 0.99×, params 1.00×.

## Read

No speedup, no regression you can trust. Pair 1 is ~1–2% down; pair 2
is ~2–3% up on GET and flat on JSON. That is the same host noise band
as the earlier find_handler A/B (plaintext on `cython-core` has printed
from ~130k to ~144k on this VM today).

Dropping exception mapping and auto-`end()` does not show up on the
wrk GET path: those handlers already write a complete 200. The Cython
308 write is off the bench (wrk hits `/plaintext`, `/json`, `/params`,
none with a trailing slash).

758 tests passed on `ae10c94`.
