# Stario 4.1.1 × asyncio event loops

Same Stario checkout, same HTTP app (`benchmarks/server/apps/stario_app.py`),
same four cases as `benchmarks/server/`. Only `STARIO_LOOP` changes. HTML
compare/micro are omitted — they never enter an event loop.

Median requests/sec ± sample stdev. `wrk -t 2 -c 128 -d 10s`, 1 warmup + 3
measured samples. No non-2xx or socket errors in any `wrk` run.

| Loop | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| asyncio | 47,239 ± 1,219 | 51,822 ± 3,123 | 51,221 ± 1,509 | 47,632 ± 255 |
| uvloop | 73,650 ± 261 | 71,568 ± 803 | 69,988 ± 1,224 | 55,698 ± 355 |
| zuvloop | **84,234 ± 849** | **80,414 ± 802** | **77,365 ± 1,314** | **62,533 ± 1,702** |
| rloop | 55,252 ± 4,749 | 51,855 ± 1,120 | 46,879 ± 1,551 | 39,758 ± 3,864 |
| rsloop | 53,625 ± 1,655 | 47,575 ± 1,140 | 46,136 ± 468 | 37,686 ± 633 |
| uringcore | skip | skip | skip | skip |

Relative to stdlib asyncio (median rps):

| Loop | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| asyncio | 1.00× | 1.00× | 1.00× | 1.00× |
| uvloop | 1.56× | 1.38× | 1.37× | 1.17× |
| zuvloop | 1.78× | 1.55× | 1.51× | 1.31× |
| rloop | 1.17× | 1.00× | 0.92× | 0.83× |
| rsloop | 1.14× | 0.92× | 0.90× | 0.79× |

## Packages

| Loop | Version | Implementation |
| --- | --- | --- |
| asyncio | stdlib | CPython `SelectorEventLoop` |
| uvloop | 0.22.1 | libuv / Cython ([MagicStack/uvloop](https://github.com/MagicStack/uvloop)) |
| zuvloop | 0.0.13 | libuv / Zig ([Kludex/zuvloop](https://github.com/Kludex/zuvloop)) |
| rloop | 0.5.0 | Rust / mio ([gi0baro/rloop](https://github.com/gi0baro/rloop); experimental) |
| rsloop | 0.1.46 | Rust / io_uring ([RustedBytes/rsloop](https://github.com/RustedBytes/rsloop)) |
| uringcore | — | Did not install on CPython 3.14 (PyO3 max 3.13 in 0.9.1) |

Also considered, not run here:

- **winloop** — uvloop port for Windows; this host is Linux.
- **uringcore** — Linux io_uring loop; current PyPI release does not build on 3.14.

## Machine

| | |
| --- | --- |
| Host | KVM guest, hostname `cursor` |
| OS | Ubuntu 24.04.4 LTS (`6.12.94+`, x86_64, glibc 2.39) |
| CPU | Intel Xeon Processor, 4 cores / 4 threads, 1 socket |
| Memory | 16 GiB |
| Python | CPython 3.14.7 (uv build, Clang 22.1.3) |
| Client | wrk 4.1.0 (`debian/4.1.0-4build2`, epoll) |
| Stario | 4.1.1 (`04ffa87`) |

Raw run: `benchmarks/loops/results/20260901T161526Z/` (local; gitignored).

Re-run:

```bash
benchmarks/loops/run.sh
```
