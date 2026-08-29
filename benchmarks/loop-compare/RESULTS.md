# main × cython-core × uvloop × zuvloop

One-host comparison of every Stario benchmark we have, with
[uvloop](https://github.com/MagicStack/uvloop) 0.22.1 vs
[zuvloop](https://github.com/Kludex/zuvloop) 0.0.12.

Relative numbers — not lab-grade absolutes. Same knobs as
`cython-core` `baseline-20260828.md`.

## Headline

| Question | Answer |
| --- | --- |
| Does zuvloop help **main** (Python httptools)? | **Yes.** ~**1.09–1.14×** on GET and small POST. Large buffered uploads are even. Streaming 2MB is **1.17×**. |
| Does zuvloop help **cython-core Python** (httptools + Cython `Headers`)? | **Yes.** ~**1.08–1.14×** on GET/small POST, **~1.20×** on 2MB/multipart. |
| Does zuvloop help **cython-core Cython llhttp**? | **Split.** uvloop wins high-RPS GET/JSON (**~1.3×** zuvloop). zuvloop wins large bodies (**1.15–1.29×**). |
| Fastest GET / small JSON? | **Cython llhttp + uvloop** (plaintext **134,907** req/s). |
| Fastest large upload? | **Cython llhttp + zuvloop** (64KB **48,285**, 2MB stream **4,325**). |
| HTML / headers_micro? | Loop-independent. main and cython-core markup match. |

**If you ship Python Stario today:** `STARIO_LOOP=zuvloop` is a free ~10% on the official suite.

**If you ship `cython-core`:** keep **uvloop** for request-heavy traffic; try **zuvloop** when the workload is large bodies.

## Environment

| | |
|---|---|
| CPU | Intel Xeon (KVM), 4 vCPUs, x86_64 |
| RAM | 16 GiB, no swap |
| OS | Linux 6.12.94+ |
| Python | 3.14.7 |
| uvloop | 0.22.1 |
| zuvloop | 0.0.12 |
| Client | wrk 4.1.0, `-t2 -c128` (read/small POST), `-c32` (64KB/2MB) |
| Topology | wrk + server on `127.0.0.1` |
| main | `34250b1` (this branch: `STARIO_LOOP=zuvloop` only; HTTP hot path = `origin/main` `386ff0d`) |
| cython-core | `de72935` plus the same loop-selection patch so `STARIO_LOOP=zuvloop` is honored |
| Run | `20260829T113127Z` |

## Methodology

Official `cython-core` suite:

- `DURATION=10s` `RUNS=5` `WARMUP=1` `THREADS=2`
- `CONNECTIONS=128` `UPLOAD_CONNECTIONS=32` `ENDPOINT_TIER=all`
- One worker. Handlers compute responses per request.
- Median req/s ± sample stdev after IQR trimming (`benchmarks/server/stats.py`).
- All six Stario combinations use the **same** `cython-core` app
  (`plaintext`, `json`, `params`, plus the seven upload routes).
- HTML compare/micro and `headers_micro` do not use an event loop.

Reproduce:

```bash
git worktree add /tmp/stario-cython origin/cython-core
DURATION=10s RUNS=5 WARMUP=1 THREADS=2 \
  CONNECTIONS=128 UPLOAD_CONNECTIONS=32 \
  benchmarks/loop-compare/run.sh
benchmarks/loop-compare/run-html.sh
```

## HTTP: all six together

### Read-heavy

| Server | Plaintext | JSON | Params |
| --- | ---: | ---: | ---: |
| main Python httptools + uvloop | 73,631 ± 1,593 | 73,651 ± 1,367 | 71,314 ± 508 |
| main Python httptools + zuvloop | 83,291 ± 274 | 79,970 ± 183 | 79,352 ± 318 |
| cython-core Python httptools + uvloop | 77,939 ± 1,190 | 74,282 ± 1,084 | 72,362 ± 1,434 |
| cython-core Python httptools + zuvloop | 85,970 ± 461 | 82,919 ± 227 | 82,414 ± 127 |
| **cython-core Cython llhttp + uvloop** | **134,907 ± 1,311** | **126,232 ± 713** | **124,099 ± 2,380** |
| cython-core Cython llhttp + zuvloop | 96,911 ± 4,597 | 97,567 ± 3,544 | 92,308 ± 2,813 |

### Upload / body

| Server | Validate | Form | JSON 1KB | 64KB | 2MB buf | 2MB stream | Multipart |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| main + uvloop | 56,568 ± 364 | 60,913 ± 1,147 | 56,436 ± 1,191 | 20,590 ± 192 | 2,160 ± 240 | 3,125 ± 14 | 2,117 ± 61 |
| main + zuvloop | 62,739 ± 46 | 68,204 ± 93 | 64,285 ± 111 | 20,250 ± 26 | 2,074 ± 52 | 3,658 ± 14 | 2,099 ± 71 |
| cython-core Python + uvloop | 58,323 ± 408 | 62,980 ± 1,083 | 59,148 ± 760 | 20,789 ± 127 | 2,145 ± 51 | 3,184 ± 21 | 2,187 ± 51 |
| cython-core Python + zuvloop | 64,942 ± 138 | 70,180 ± 1,239 | 64,122 ± 847 | 21,528 ± 220 | 2,579 ± 64 | 3,843 ± 11 | 2,605 ± 9 |
| cython llhttp + uvloop | **106,354 ± 468** | 127,645 ± 974 | **110,442 ± 411** | 37,312 ± 146 | 3,546 ± 19 | 3,765 ± 2 | 3,736 ± 7 |
| cython llhttp + zuvloop | 81,910 ± 2,001 | **135,779 ± 2,282** | 80,828 ± 2,676 | **48,285 ± 1,261** | **4,278 ± 18** | **4,325 ± 19** | **4,285 ± 17** |

Bold is the highest cell in that column.

## zuvloop / uvloop (same stack)

| Endpoint | main | cython-core Python | Cython llhttp |
| --- | ---: | ---: | ---: |
| Plaintext | **1.13×** | **1.10×** | 0.72× |
| JSON | **1.09×** | **1.12×** | 0.77× |
| Params | **1.11×** | **1.14×** | 0.74× |
| Validate JSON | **1.11×** | **1.11×** | 0.77× |
| Form POST | **1.12×** | **1.11×** | **1.06×** |
| JSON 1KB | **1.14×** | **1.08×** | 0.73× |
| Octet 64KB | 0.98× | **1.04×** | **1.29×** |
| Octet 2MB (buffer) | 0.96× | **1.20×** | **1.21×** |
| Octet 2MB (stream) | **1.17×** | **1.21×** | **1.15×** |
| Multipart 2MB | 0.99× | **1.19×** | **1.15×** |

Python stacks: zuvloop is a consistent win except main's large buffered POSTs
(noise / already bandwidth-bound). Cython llhttp **regresses** on the
high-RPS path and **gains** once the body is tens of kilobytes.

## vs main + uvloop (the published default)

| Endpoint | main + zuvloop | core Python + uvloop | core Python + zuvloop | llhttp + uvloop | llhttp + zuvloop |
| --- | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 1.13× | 1.06× | 1.17× | **1.83×** | 1.32× |
| JSON | 1.09× | 1.01× | 1.13× | **1.71×** | 1.32× |
| Params | 1.11× | 1.01× | 1.16× | **1.74×** | 1.29× |
| Validate | 1.11× | 1.03× | 1.15× | **1.88×** | 1.45× |
| Form | 1.12× | 1.03× | 1.15× | 2.10× | **2.23×** |
| JSON 1KB | 1.14× | 1.05× | 1.14× | **1.96×** | 1.43× |
| 64KB | 0.98× | 1.01× | 1.05× | 1.81× | **2.35×** |
| 2MB buf | 0.96× | 0.99× | 1.19× | 1.64× | **1.98×** |
| 2MB stream | 1.17× | 1.02× | 1.23× | 1.21× | **1.38×** |
| Multipart | 0.99× | 1.03× | 1.23× | 1.76× | **2.02×** |

`cython-core` Python is a few percent above `main` on uvloop because
response `Headers` already come from the Cython extension. The loop
swap is the larger move on that stack.

## Winner per endpoint

| Endpoint | Winner | Median req/s |
| --- | --- | ---: |
| Plaintext | Cython llhttp + uvloop | 134,907 |
| JSON | Cython llhttp + uvloop | 126,232 |
| Params | Cython llhttp + uvloop | 124,099 |
| Validate JSON | Cython llhttp + uvloop | 106,354 |
| Form POST | Cython llhttp + zuvloop | 135,779 |
| JSON 1KB | Cython llhttp + uvloop | 110,442 |
| Octet 64KB | Cython llhttp + zuvloop | 48,285 |
| Octet 2MB (buffer) | Cython llhttp + zuvloop | 4,278 |
| Octet 2MB (stream) | Cython llhttp + zuvloop | 4,325 |
| Multipart 2MB | Cython llhttp + zuvloop | 4,285 |

## Notes on the Cython + zuvloop GET regression

wrk on plaintext (one measured sample):

| | Cython + uvloop | Cython + zuvloop |
| --- | ---: | ---: |
| Latency avg | 0.94 ms | 1.25 ms |
| Latency stdev | 96 µs | 312 µs |
| Req/s (that sample) | 135,164 | 101,454 |

No socket errors. The Cython protocol was built and tuned against uvloop
(hard-coded default in `stario_cython.serve`). zuvloop's handle layout,
write batching, and ready-queue behavior are different; the high-RPS
path looks more sensitive to that than the Python httptools protocol.

Large-body wins on the same binary are consistent with zuvloop's
vectored write / read path (the zuvloop README's bulk-stream and
uvicorn-body rows).

## HTML generation (loop-independent)

`benchmarks/html/compare.py` — 50-row product page, µs/render, best of 7×2000.
Lower is better.

| Renderer | main | cython-core |
| --- | ---: | ---: |
| stario @baked | **40.2** | **40.0** |
| jinja2 (compiled, autoescape) | 105.3 | 105.3 |
| stario naive | 251.8 | 253.4 |
| tdom (components) | 854.0 | 827.6 |
| tdom (naive t-string) | 872.0 | 861.7 |
| htpy | 919.8 | 923.5 |
| dominate | 1223.4 | 1206.4 |

Markup is unchanged between branches. Spread is run noise.

`benchmarks/html/micro.py` — µs/call, best of 7. Event loop unused.

| Case | main | cython-core | core / main |
| --- | ---: | ---: | ---: |
| Div() cached empty | 0.061 | 0.060 | 0.98× |
| Div('text') | 0.366 | 0.364 | 0.99× |
| Div({'class': 'card'}) | 0.784 | 0.795 | 1.01× |
| Div(classes(5 tokens)) | 1.499 | 1.508 | 1.01× |
| Div(data(2 keys)) | 1.152 | 1.203 | 1.04× |
| Div(styles(2 props)) | 1.673 | 1.709 | 1.02× |
| Div(classes(3 conditional)) | 1.066 | 1.093 | 1.03× |
| @baked positional | 0.565 | 0.576 | 1.02× |
| @baked keyword | 0.577 | 0.589 | 1.02× |
| @baked + render | 0.753 | 0.772 | 1.03× |
| render small prebuilt tree | 0.712 | 0.708 | 0.99× |

## Request-header micro (`headers_micro.py`, cython-core only)

100,000 requests × 7 repeats. Median **nanoseconds** per request. No event loop.

Arena linear lookup stays the win when the handler reads headers
(one read at 8 fields: **169 ns** vs lazy dict **478 ns**, **−65%**).
No-read cases stay ~140–150 ns.

| fields | workload | eager | lazy | arena hash | arena linear | adaptive-3 | linear vs lazy |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | no application reads | 453 | 140 | 145 | 143 | 150 | +1.6% |
| 8 | one arbitrary read | 479 | 478 | 177 | 169 | 177 | −64.6% |
| 8 | one missing optional read | 467 | 468 | 157 | 153 | 159 | −67.3% |
| 8 | three distinct reads | 524 | 520 | 231 | 212 | 523 | −59.3% |
| 8 | same header 8x | 651 | 644 | 355 | 336 | 639 | −47.8% |
| 8 | same missing header 8x | 586 | 583 | 268 | 240 | 568 | −58.8% |
| 8 | same header 64x | 1809 | 1811 | 1788 | 1664 | 1850 | −8.1% |
| 8 | single Cookie getlist | 500 | 495 | 196 | 190 | 195 | −61.6% |
| 10 | three Cookie lines getlist | 636 | 639 | 264 | 265 | 263 | −58.5% |
| 10 | three Cookie lines getlist 3x | 737 | 734 | 437 | 440 | 787 | −40.1% |
| 16 | no application reads | 889 | 269 | 293 | 269 | 295 | +0.2% |
| 16 | one arbitrary read | 920 | 918 | 312 | 291 | 309 | −68.3% |
| 16 | one missing optional read | 915 | 909 | 300 | 278 | 299 | −69.4% |
| 16 | three distinct reads | 971 | 979 | 375 | 352 | 962 | −64.0% |
| 16 | same header 8x | 1103 | 1106 | 512 | 465 | 1082 | −57.9% |
| 16 | same missing header 8x | 1037 | 1026 | 421 | 375 | 1007 | −63.4% |
| 16 | same header 64x | 2238 | 2240 | 1952 | 1809 | 2285 | −19.2% |
| 16 | single Cookie getlist | 952 | 951 | 336 | 323 | 336 | −66.0% |
| 18 | three Cookie lines getlist | 1104 | 1112 | 407 | 402 | 412 | −63.8% |
| 18 | three Cookie lines getlist 3x | 1210 | 1200 | 593 | 598 | 1248 | −50.2% |
| 32 | no application reads | 1934 | 514 | 562 | 522 | 563 | +1.6% |
| 32 | one arbitrary read | 1994 | 1979 | 594 | 540 | 597 | −72.7% |
| 32 | one missing optional read | 1973 | 1965 | 589 | 545 | 591 | −72.3% |
| 32 | three distinct reads | 2031 | 2019 | 648 | 605 | 1980 | −70.0% |
| 32 | same header 8x | 2149 | 2144 | 795 | 724 | 2116 | −66.2% |
| 32 | same missing header 8x | 2091 | 2087 | 748 | 669 | 2064 | −67.9% |
| 32 | same header 64x | 3294 | 3299 | 2216 | 2080 | 3308 | −37.0% |
| 32 | single Cookie getlist | 2010 | 2001 | 632 | 571 | 632 | −71.5% |
| 34 | three Cookie lines getlist | 2205 | 2189 | 692 | 667 | 690 | −69.5% |
| 34 | three Cookie lines getlist 3x | 2300 | 2294 | 910 | 896 | 2375 | −61.0% |

## vs the 2026-08-28 Cython baseline

Same machine class and knobs. That snapshot was Cython + **uvloop** only
(`de72935` lineage, `20260828T193136Z`): plaintext **132,903**, validate
**112,068**. This run's Cython + uvloop row (plaintext **134,907**,
validate **106,354**) sits in the same band.

Competitor rows (Granian RSGI, Socketify, …) were not re-run. See
`cython-core` `benchmarks/server/baseline-20260828.md`.
