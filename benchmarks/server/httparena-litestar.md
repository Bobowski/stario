# Stario vs Litestar

[Litestar](https://github.com/litestar-org/litestar) is Class B: a
real Python framework on uvicorn (HttpArena: uvloop + httptools,
msgspec, one worker per core). Composite **236**, 2nd Python on
HttpArena behind aiohttp 240.

On this machine, same 10 honest routes, production uvicorn
(`production-peers.md`):

| | Plaintext | Validate |
| --- | ---: | ---: |
| Stario Cython 1w | **129,297** | **105,918** |
| Litestar 1w | 30,353 | 19,796 |
| Stario Cython ×4 | **282,133** | **244,755** |
| Litestar ×4 | 100,635 | 71,323 |

**~4.3×** (1 worker) and **~2.8×** (4 workers) on plaintext. Litestar
is slower than aiohttp on this suite (51k / 155k). HttpArena’s near-tie
with aiohttp is composite + completeness on their 64-core box, not a
speed tie on our routes.

Do not put Litestar in Class A. Do not use their HttpArena 316k
baseline next to our 282k ×4.
