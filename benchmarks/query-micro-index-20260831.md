# Query index vs scan vs eager (2026-08-31)

Same host as the Cython HTTP work. Pooled `ParsedQuery` rebound with
`__init__(raw)` each request. Median nanoseconds per request.

- **index** — production `get`: memcpy query, fill C name/value spans, memcmp
  names, decode the requested value.
- **scan** — previous production path (`_get_scan`): walk `&` / `=` each get,
  decode names until a match.
- **eager** — `_get_eager`: decode every name and value into Python lists,
  then list-scan.

Main tables: `80,000` × `7`. K×N matrix: `40,000` × `5`.

## Where index sits

| Workload | vs previous scan | vs eager | Notes |
| --- | --- | --- | --- |
| 1 get **first** key | **slower** (1.06–3.87×) | faster (0.85–0.20×) | Scan stops after the first pair (~120 ns flat). Index always splits the whole string. |
| 1 get last / missing | **faster** (~0.55–0.80×) | faster (~0.3–0.8×) | Scan decodes every name. Index memcmps. |
| 2 distinct gets | faster (~0.6–0.9×) | faster | Crossover vs scan is already at K=2. |
| 3 distinct gets | faster (~0.5–0.7×) | faster | |
| **10 distinct gets** | **~0.49× scan** | **0.56–0.93× eager** | The intended working set. ~1.3–1.9 µs. |
| get every key | ~0.5× scan | **tie** (~0.96–1.0×) | Index pays memcpy; then same decode-all cost as eager. |
| getlist one key | faster (~0.5–0.9×) | faster | |

**K = 1 first key** is the only place the old linear walk wins. Everywhere
else — last key, missing, 2+ named reads, 10 reads, dump-all — index is
ahead of scan and at worst tied with eager.

## One named `get`

| pairs | first index | scan | eager | last index | scan | eager |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 128 | 121 | 151 | 116 | 117 | 148 |
| 5 | 155 | 122 | 357 | 171 | 224 | 359 |
| 8 | 176 | 121 | 457 | 212 | 313 | 499 |
| 16 | 229 | 117 | 840 | 327 | 527 | 951 |
| 32 | 343 | 117 | 1541 | 548 | 950 | 1753 |
| 48 | 456 | 118 | 2228 | 758 | 1371 | 2540 |

First-key scan stays ~120 ns through 48 pairs. Index grows with N because it
builds the full span table on first touch (memcpy + one walk). At 48 pairs
that is still **0.20×** eager (456 vs 2228 ns).

## Several `get`s on one query

| pairs | 2 first+last idx / scan / eager | 3 spread | 10 spread | every key |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 251 / 321 / 409 | 335 / 486 / 450 | — | 516 / 801 / 535 |
| 12 | 345 / 511 / 765 | 465 / 787 / 838 | 1275 / 2602 / 1367 | 1466 / 2988 / 1480 |
| 16 | 407 / 616 / 990 | 615 / 1083 / 1174 | 1351 / 2755 / 1595 | 2140 / 4803 / 2185 |
| 48 | 831 / 1444 / 2621 | 1066 / 2171 / 2782 | 1881 / 3986 / 3336 | — |

Ten named reads on 12 pairs: **1275 ns** index vs 2602 scan vs 1367 eager.

## Distinct params read (K) × pairs on the wire (N)

Keys spaced through the string (first … last). Cell is index/other.
`i` = index wins or tie (≤1.05). `s` / `e` = scan / eager ahead.

### index / scan

| N pairs \\ K reads | 1 | 2 | 3 | 4 | 6 | 8 | 10 | 12 | 16 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 1.34s | 0.65i | 0.59i | 0.57i | 0.53i | 0.55i | — | — | — |
| 16 | 1.86s | 0.66i | 0.60i | 0.53i | 0.49i | 0.48i | 0.47i | 0.45i | 0.44i |
| 24 | 2.33s | 0.63i | 0.55i | 0.51i | 0.46i | 0.44i | 0.43i | 0.41i | 0.41i |
| 32 | 2.45s | 0.61i | 0.54i | 0.48i | 0.44i | 0.42i | 0.40i | 0.39i | 0.38i |
| 48 | 3.57s | 0.58i | 0.50i | 0.45i | 0.41i | 0.39i | 0.37i | 0.36i | 0.35i |

K=1 here is the first key (scan’s best case). From **K = 2** index is
ahead at every N.

### index / eager

| N pairs \\ K reads | 1 | 2 | 3 | 4 | 6 | 8 | 10 | 12 | 16 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 0.36i | 0.52i | 0.61i | 0.71i | 0.89i | 1.02i | — | — | — |
| 16 | 0.29i | 0.41i | 0.55i | 0.59i | 0.70i | 0.81i | 0.88i | 0.93i | 1.01i |
| 24 | 0.26i | 0.38i | 0.46i | 0.53i | 0.62i | 0.73i | 0.79i | 0.83i | 0.94i |
| 32 | 0.22i | 0.35i | 0.44i | 0.49i | 0.59i | 0.67i | 0.73i | 0.79i | 0.87i |
| 48 | 0.21i | 0.32i | 0.40i | 0.45i | 0.54i | 0.62i | 0.68i | 0.73i | 0.81i |

Index never loses to eager in this range. Reading every key is a tie
(memcpy + span table ≈ decode-all into Python lists).
