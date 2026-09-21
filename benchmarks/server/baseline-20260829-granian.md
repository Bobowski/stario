# Server benchmarks — 2026-08-29 (Cython vs Granian RSGI)

Same-host, this checkout after dropping cross-request caches
(`8d840b7`). Granian 2.8.2, RSGI, no framework. One worker each.
`DURATION=10s` `RUNS=5` `WARMUP=1` `THREADS=2` `CONNECTIONS=128`
`UPLOAD_CONNECTIONS=32`, IQR trim.

Two orders so the lead is not “who went first”:

| Suite | Dir | Order |
| --- | --- | --- |
| All endpoints | `20260829T144533Z` | Stario Cython → Granian |
| GET only | `20260829T150555Z` | Granian → Stario Cython |
| Upload only | `20260829T151215Z` | Granian → Stario Cython |

## Read-heavy (req/s)

| Endpoint | Order | Stario Cython | Granian RSGI | Stario / Granian |
| --- | --- | ---: | ---: | ---: |
| Plaintext | Stario first | 151,272 ± 1,830 | 115,099 ± 690 | **1.31×** |
| Plaintext | Granian first | 153,569 ± 2,100 | 117,070 ± 1,124 | **1.31×** |
| JSON | Stario first | 137,893 ± 63 | 112,758 ± 4,730 | **1.22×** |
| JSON | Granian first | 142,118 ± 446 | 120,028 ± 3,701 | **1.18×** |
| Params | Stario first | 133,955 ± 284 | 119,834 ± 4,971 | **1.12×** |
| Params | Granian first | 138,713 ± 267 | 120,072 ± 4,081 | **1.16×** |

Mean of the two orders: plaintext **1.31×**, JSON **1.20×**, params **1.14×**.

## Upload / body (req/s)

| Endpoint | Order | Stario Cython | Granian RSGI | Stario / Granian |
| --- | --- | ---: | ---: | ---: |
| Validate JSON | Stario first | 119,882 ± 5,432 | 78,693 ± 2,043 | **1.52×** |
| Validate JSON | Granian first | 117,568 ± 776 | 80,661 ± 1,142 | **1.46×** |
| Form POST | Stario first | 154,103 ± 5,571 | 86,734 ± 971 | **1.78×** |
| Form POST | Granian first | 151,209 ± 7,795 | 88,790 ± 1,708 | **1.70×** |
| JSON 1KB | Stario first | 128,090 ± 6,444 | 80,268 ± 430 | **1.60×** |
| JSON 1KB | Granian first | 124,580 ± 1,731 | 83,410 ± 926 | **1.49×** |
| Octet 64KB | Stario first | 64,470 ± 1,665 | 27,005 ± 747 | **2.39×** |
| Octet 64KB | Granian first | 63,239 ± 68 | 27,728 ± 661 | **2.28×** |
| Octet 2MB (buffer) | Stario first | 3,304 ± 8 | 2,013 ± 34 | **1.64×** |
| Octet 2MB (buffer) | Granian first | 3,389 ± 55 | 1,988 ± 19 | **1.70×** |
| Octet 2MB (stream) | Stario first | 3,530 ± 42 | 2,247 ± 21 | **1.57×** |
| Octet 2MB (stream) | Granian first | 3,589 ± 9 | 2,278 ± 15 | **1.58×** |
| Multipart 2MB | Stario first | 3,343 ± 90 | 2,600 ± 25 | **1.29×** |
| Multipart 2MB | Granian first | 3,393 ± 9 | 2,639 ± 10 | **1.29×** |

Mean of the two orders: validate **1.49×**, form **1.74×**, JSON 1KB **1.55×**,
64KB **2.33×**, 2MB buffer **1.67×**, 2MB stream **1.57×**, multipart **1.29×**.

Stario is ahead on every endpoint in both orders.

## Caveat: this-morning Granian

Same VM, same knobs, morning three-way (`20260829T085500Z`): Granian
plaintext **146,463**. This-afternoon Granian is **115–117k** (~0.80×
that print) even when it runs first. Stario GET stayed at **151–154k**,
which is also only a few percent above the morning Granian number
(host GET noise is several k req/s).

So: vs **Granian as it ran in these pairs**, the GET lead is large and
stable. vs **this morning’s Granian absolute**, GET is a thin noisy
lead, not a locked 1.31×. Upload gaps are large enough that a 20%
Granian recovery would not flip them.
