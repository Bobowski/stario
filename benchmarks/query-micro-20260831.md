# Query scan vs eager parse-all (2026-08-31)

Same host as the Cython HTTP work. `80,000` requests × `7` repeats. Pooled
`ParsedQuery` rebound with `__init__(raw)` each request. Median nanoseconds
per request. Ratio `< 1` means scan is faster.

Eager is `_get_eager`: fill both Python arrays (every name and value), then
list-scan. Scan is production `get` / `getlist`.

## One named `get`

| pairs | first key scan | eager | last key scan | eager |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 95 | 146 | 96 | 144 |
| 3 | 96 | 236 | 152 | 248 |
| 5 | 98 | 346 | 206 | 359 |
| 8 | 99 | 471 | 298 | 527 |
| 16 | 97 | 1000 | 517 | 1002 |
| 32 | 96 | 1689 | 957 | 1804 |
| 48 | 96 | 2491 | 1391 | 2758 |

First-key `get` stays ~100 ns through 48 pairs. Eager grows with pair count
(decode every value). Last-key `get` grows linearly (~28 ns/pair) and is still
about **0.5–0.6×** eager at 48 pairs. Missing key is the same shape as last key
(must walk every name) and still ~0.5× eager (names only, no unused values).

Percent-encoded values (`hello world %`) widen the gap: first-key scan stays
~146 ns; eager at 48 pairs is 4619 ns (**0.03×**).

## Several `get`s on one query

| pairs | 2 gets first+last | eager | 3 gets spread | eager | every key | eager |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 159 (0.88×) | 181 | 231 (1.18×)* | 196 | 99 (0.68×) | 146 |
| 3 | 222 (0.79×) | 280 | 337 (1.09×) | 309 | 336 (1.10×) | 305 |
| 5 | 276 (0.72×) | 384 | 422 (0.99×) | 429 | 700 (1.40×) | 499 |
| 8 | 373 (0.68×) | 550 | 560 (0.97×) | 575 | 1423 (1.72×) | 829 |
| 16 | 590 (0.59×) | 1000 | 893 (0.84×) | 1058 | 4840 (2.24×) | 2161 |

\* 1 pair × “3 gets spread” reads the same key three times: scan walks thrice,
eager fills once.

`getlist` of one repeated key: scan wins at every size (0.76× at 1 pair, 0.52×
at 48).

## Crossover

Scan keeps an advantage until you **read most of the pairs**:

- **1 `get`** (first, last, missing, encoded): scan wins through **48+ pairs**.
  No crossover in this range. This is the usual app path (`id`, `cellId`,
  `datastar`).
- **2 distinct `get`s**: scan wins through **48+ pairs**.
- **3 distinct `get`s**: roughly **tied** at 3–8 pairs (eager 0–10% ahead at
  3–4; scan pulls ahead again as unused values get more expensive). Not a
  clean “eager wins from here on.”
- **`get` every key**: eager wins from **3 pairs** and the gap grows (1.4× at
  5, 2.2× at 16). That is `items()` / `as_dict` territory; those paths already
  parse everything.

So: keep scan-on-`get`. Eager parse-all only wins when the handler reads
almost every pair. Typical 1–3 named reads on a short or fat query stay on
the scan side.
