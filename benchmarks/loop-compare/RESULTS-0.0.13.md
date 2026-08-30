# zuvloop 0.0.13 retest

Same trees and knobs as [RESULTS.md](RESULTS.md) (zuvloop **0.0.12**, run
`20260829T113127Z`). This run: **zuvloop 0.0.13**, `20260830T170359Z`.

| | |
|---|---|
| uvloop | 0.22.1 (unchanged) |
| zuvloop | **0.0.13** (was 0.0.12) |
| main | same HTTP hot path as before |
| cython-core | `de72935` |
| Knobs | `RUNS=5 WARMUP=1 DURATION=10s` `-t2 -c128` / `-c32` |

**Headline:** 0.0.13 fixes the Cython GET regression. Python is still ~**1.15×**
zuvloop over uvloop. Cython llhttp + zuvloop 0.0.13 is now the fastest row on
this machine (plaintext **146,667**).

Treat same-run Cython+uvloop ratios with care: that row was noisy this time
(plaintext 102k ± 15k vs 135k ± 1.3k yesterday). The honest Cython compare is
**zuvloop 0.0.13 vs yesterday’s quiet uvloop baseline**.

## Python httptools (main) — this run

| Endpoint | uvloop | zuvloop 0.0.13 | 0.0.13 / uvloop | vs zuvloop 0.0.12 |
| --- | ---: | ---: | ---: | ---: |
| Plaintext | 72,771 | **86,185** | **1.18×** | 1.03× |
| JSON | 70,214 | **81,783** | **1.16×** | 1.02× |
| Params | 72,875 | **79,057** | **1.08×** | 1.00× |
| Validate | 55,983 | **64,607** | **1.15×** | 1.03× |
| Form POST | 58,968 | **71,048** | **1.20×** | 1.04× |
| JSON 1KB | 55,403 | **64,313** | **1.16×** | 1.00× |
| Octet 64KB | 20,643 | 20,642 | 1.00× | 1.02× |
| 2MB buffer | 2,002 | **2,172** | **1.08×** | 1.05× |
| 2MB stream | 3,000 | **3,649** | **1.22×** | 1.00× |
| Multipart | **2,077** | 1,921 | 0.92× | 0.92× |

Python story is unchanged: ~10–20% on GET/small POST. 0.0.13 is within a few
percent of 0.0.12 on this stack.

## Cython llhttp — this run

| Endpoint | uvloop (noisy this run) | zuvloop 0.0.13 | same-run ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 102,498 ± 14,772 | **146,667 ± 2,983** | 1.43× |
| JSON | 125,619 ± 12,811 | **137,911 ± 3,566** | 1.10× |
| Params | 96,493 ± 4,105 | **119,027 ± 3,260** | 1.23× |
| Validate | 83,182 ± 6,506 | **101,365 ± 6,982** | 1.22× |
| Form POST | 127,091 ± 25,643 | **140,246 ± 1,407** | 1.10× |
| JSON 1KB | **115,950 ± 12,181** | 110,478 ± 3,382 | 0.95× |
| Octet 64KB | 36,055 ± 2,664 | **53,420 ± 632** | **1.48×** |
| 2MB buffer | 2,599 ± 16 | **4,064 ± 8** | **1.56×** |
| 2MB stream | 2,756 ± 259 | **4,042 ± 27** | **1.47×** |
| Multipart | 3,015 ± 106 | **4,073 ± 65** | **1.35×** |

## Cython: 0.0.13 vs the quiet 0.0.12 capture

Better baseline for “did 0.0.13 change the Cython verdict?”

| Endpoint | Cython + uvloop (0.0.12 run) | Cython + zuvloop 0.0.12 | Cython + zuvloop 0.0.13 | 0.0.13 / old uvloop | 0.0.13 / 0.0.12 zuvloop |
| --- | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 134,907 | 96,911 | **146,667** | **1.09×** | **1.51×** |
| JSON | 126,232 | 97,567 | **137,911** | **1.09×** | **1.41×** |
| Params | **124,099** | 92,308 | 119,027 | 0.96× | **1.29×** |
| Validate | **106,354** | 81,910 | 101,365 | 0.95× | **1.24×** |
| Form POST | 127,645 | 135,779 | **140,246** | **1.10×** | 1.03× |
| JSON 1KB | 110,442 | 80,828 | **110,478** | 1.00× | **1.37×** |
| Octet 64KB | 37,312 | 48,285 | **53,420** | **1.43×** | **1.11×** |
| 2MB buffer | 3,546 | **4,278** | 4,064 | **1.15×** | 0.95× |
| 2MB stream | 3,765 | **4,325** | 4,042 | **1.07×** | 0.93× |
| Multipart | 3,736 | **4,285** | 4,073 | **1.09×** | 0.95× |

0.0.12 lost GET by ~**0.72×**. 0.0.13 is even-to-ahead of the old uvloop
Cython default on GET, and still ahead on large bodies.

## What changed vs 0.0.12

| Stack | 0.0.12 vs uvloop | 0.0.13 vs uvloop (this run) | 0.0.13 vs quiet uvloop |
| --- | --- | --- | --- |
| Python (main) GET | ~1.09–1.14× | ~1.08–1.18× | same |
| Cython llhttp GET | **0.72–0.77×** (lost) | 1.10–1.43× (noisy uvloop) | **~0.95–1.09×** (even / slight win) |
| Cython llhttp large body | 1.15–1.29× | 1.35–1.56× | **1.07–1.43×** |

## Verdict

0.0.13 is a real Cython GET fix relative to 0.0.12 (plaintext **97k → 147k**
on the same binary). It no longer looks like a high-RPS regression.

This run’s Cython+uvloop row is too noisy to crown a new default from
same-run ratios alone. Against yesterday’s quiet uvloop Cython numbers,
0.0.13 is **slightly ahead on plaintext/JSON** and **clearly ahead on 64KB+**.

HTML / `headers_micro` were not re-run (no event loop).
