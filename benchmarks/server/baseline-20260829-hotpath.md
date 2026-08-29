# Server benchmarks — 2026-08-29 (Cython GET hot path)

Cross-request caches on the same connection (one-slot `text()` encode,
cached `respond()` header block, connection `(path, method)` handler slot,
`Router.routes_version`) were removed. They were over-optimization of the
keep-alive repeat. The standard request path is what we keep fast:

- Skip upload/Event state when there is no body (`mark_nobody`).
- `respond()` writes interned status / Date / type / length / body pieces
  via `writelines` (no join, no retained header blob).
- Arm idle timeout as “deadline 0” and let the Date-tick sweeper fill
  `now + keep_alive`. No `loop.time()` on the wrk GET path.
- Skip `on_handler_done` when the eager task already completed a
  response under the NoOp tracer.

Earlier numbers on this page (~1.20× vs `cython-core`) included the
removed caches and are not comparable. Re-measured figures land in a
follow-up note after the A/B rerun.
