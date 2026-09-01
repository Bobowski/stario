# Changelog

All notable changes to Stario are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Breaking changes

- `App.on_error` and exception-type mapping are gone. Uncaught handler
  exceptions are logged. If the handler sent nothing, the framework writes
  **500**; a response already on the wire is not rewritten. Handlers must
  write a complete response (`respond` / `end`) or use
  `stario.http.middleware.catch_errors` to map app exceptions.
- **`HttpException` removed.** Body read failures raise `RequestBodyError`
  (408/413). Map them with `catch_request_body_errors()` or custom middleware.
- Route handlers must be `async def` (or a callable whose `__call__` is async).
- The HTTP protocol schedules `find_handler` then `create_task(handler(c, w))`
  instead of `create_task(app(c, w))`. Trailing-slash 308 is written inline in
  the Cython protocol (no handler task).

### Added

- `stario.http.middleware.catch_errors` — wrap handlers so listed exceptions
  become HTTP responses when nothing was sent yet. Presets:
  `catch_request_body_errors()` and `respond_request_body_error`.
- Cython HTTP/2 via nghttp2 on the same connection class. Switch once per socket (TLS ALPN `h2` or the cleartext connection preface). Responses go out as frames, not HTTP/1.1 text.
- Direct TLS: `ServerConfig(ssl=…)` or `STARIO_SSL_CERTFILE` / `STARIO_SSL_KEYFILE`. Context is TLS 1.2+ with ALPN `h2`, `http/1.1`.

### Changed

- Handler-task finish is `stario.http.invoke.on_handler_done`: log, write 500
  if nothing was sent, abort if a body was started but not finished, close
  the span. No auto-`end()`. A write-then-raise still logs (`Handler failed`);
  the response already on the wire is not rewritten.
- Every request that writes an HTTP status gets a started-and-ended span:
  handler responses, trailing-slash 308, and protocol 400 / 413 / 431 / 429
  (Cython) / 503 (Python pipeline). Protocol outcomes are not `fail`ed.
  `NoOpSpan` still skips start/end. Idle timeout and `connection_lost` with
  no status still do not create a span.
- Cython GET path: skip upload state when there is no body (`mark_nobody`),
  and arm idle timeouts on the Date-tick sweeper instead of `loop.time()`
  per keep-alive request.
- Cython uploads: Content-Length bodies ≤ 256 KiB dispatch after the
  message is complete (`body()` is already bytes). `stream()` with a known
  Content-Length yields `min(length, 256 KiB)` instead of a fixed 64 KiB.
- HTTP/2 POST without Content-Length is END_STREAM-delimited (like H1
  chunked): the exchange stays armed for DATA. `mark_nobody` is only for
  HEADERS that already ended the stream with no body. Recycle clears
  nghttp2 stream user_data so a pooled exchange cannot ingest another
  stream's DATA. Duplicate `:method` / `:path` / `:authority` are RST;
  `:authority` and `Host` must be equal (the second copy is not stored).
- `ParsedQuery` first read fills a C name/value span table (like request
  headers). Plain ASCII names stay on the original bytes (no memcpy);
  names that need `+` / `%XX` unquote still copy. Typical ASCII names
  compare with `memcmp`; values decode on `get`. `as_dict` / `as_lists`
  still materialize everything for forms / Pydantic. HTTP/1 stays on
  [llhttp](https://github.com/nodejs/llhttp): picohttpparser was tried on
  the same `HttpProtocol` and is ~2.5× in a parser-only microbench, but
  not noticeably faster end-to-end (GET ~even, small POST behind). llhttp
  is also the same incremental-callback model as nghttp2.
- HTTP/1 and HTTP/2 share the core dispatch rule (empty GET /
  `mark_nobody` at headers; small POST ≤256KiB waits for complete; H2
  no-CL POST dispatches at headers so `stream()` can start). HTTP/2
  receive window is 1MiB per stream / 4MiB per connection. Recv credit is
  submitted as `WINDOW_UPDATE` when a stream ends and after each
  `mem_recv` — nghttp2 `consume()` only emits a frame at 50% of the
  window, which stalls keep-alive small POSTs. Mid-body updates are still
  batched. Outbound DATA uses nghttp2 `NO_COPY` into `h2_out`; responses
  queued during `mem_recv` flush once.
- Trailing-slash URLs are not stored in the 256-slot path cache (they
  308). Accept-Encoding is not scanned when brotli and gzip are both
  off. HTTP/2 header names are memcpy'd (RFC 9113 lowercase). Host is
  normalized in C and prefetched when `host_routing`.
- HTTP/2 applies the same `max_header_bytes` budget as HTTP/1 (name +
  value octets per stream). Oversize requests get **431** on that stream,
  not a connection close. HTTP/1 431 on a GET (no body) keeps the
  connection. Declared oversize HTTP/2 bodies get **413** on that stream.
  HTTP/1 413 with a declared Content-Length at or under 256 KiB keeps
  the connection and discards the body; larger declared lengths still
  close. Keep-alive 413/431 then arm the idle timer (they do not leave
  the first-request header timer running). HTTP/2 DATA that exceeds
  `max_body_bytes` after dispatch is a
  stream **413** (or `RequestBodyError` in the handler) and does not
  close the multiplexed connection. Incomplete HTTP/2 HEADERS time out
  per stream (RST CANCEL) without tearing down multiplexed neighbors.
  `SETTINGS_MAX_HEADER_LIST_SIZE` is advertised; the HPACK table size is
  the 4KiB spec default (`SETTINGS_HEADER_TABLE_SIZE=4096`); RST-stream
  flood is rate-limited (burst 100 / 33 per second); CONTINUATION frames
  are capped from the header budget; closed streams are not retained
  (`SETTINGS_NO_RFC7540_PRIORITIES`).

## 4.1.0 - 2026-08-17

### Added

- `Route` — one HTTP method on one `UrlPath`. Declare endpoints with `Route.get` / `Route.post` / …, register them with `app.add(route, handler)`, and emit Datastar fetches with `at.fetch(route, params)`. `app.handle(method, path, handler)` and `app.get` / `app.post` stay the path + method contract. `at.fetch` reads the method from the `Route` and builds the URL with the same `href()` contract (`params`, `query=`, `fragment=`). `UrlPath` stays the method-free location for composition, `href()`, and middleware prefixes.
- HTTP `QUERY` ([RFC 10008](https://www.rfc-editor.org/rfc/rfc10008.html)) — `Route.query`, `app.query`, and `TestClient.query`. Safe and idempotent; the request body carries the query. `at.fetch` does not emit `@query` (Datastar has no such action).
- `STARIO_TRACER=module` — import the module and call `make_tracer()`. Use `module:callable` when the factory has another name.

## 4.0.1 - 2026-07-17

### Fixed

- `stario watch` on Windows — `subprocess.list2cmdline` for reload subprocess quoting; POSIX `shlex.quote` broke watchfiles spawn.

### Changed

- `debug_inspector()` — draggable signal overlay (`@baked`, yellow debug chrome, pointer capture, `data-ignore-morph`). Bottom-right only; `position=` removed. Tiles example includes it.

## 4.0.0 - 2026-06-25

Major release from 3.4. Delete old `stario-traces.sqlite3` files before upgrading — there is no in-place SQLite migration.

### Breaking changes

**Bootstrap and app lifecycle**

- `bootstrap(app, span)` — async generator with a single `yield`: startup before `yield`, teardown after.
- `App()` — requires a running event loop; exposes `shutdown` for server drain.
- `App.on_error` — handlers must be `async def`.

**Routing and URLs**

- Register full path patterns on `App`; scope middleware with `app.use(pattern, *middleware)`.
- `app.not_found(pattern, handler)` and `app.method_not_allowed(pattern, handler)` — prefix-scoped 404 / 405 handlers (inherited down the route branch).
- Build URLs with `UrlPath(...).href()` or `AssetManifest.href()`.
- `UrlPath` — typed path patterns and link generation; optional `host=` for host-aware routes (`UrlPath("/users", host="api.example.com")` or host placeholders such as `{tenant}.example.com`).

**Static assets**

- `AssetManifest(directory, …)` — scan, fingerprint, and `href()` at import time.
- `StaticAssets(manifest, …).register(app)` — serve and pre-compress in bootstrap.
- Hidden files excluded by default; pass `include_hidden=True` when dotfiles are intentional.

**HTTP exceptions and errors**

- `HttpException` — 4xx/5xx response bodies only; use `RedirectException` for 3xx and `responses.*` for 2xx.
- `RedirectException` — standalone type; `location` holds the target URL.
- Default `on_error` handlers call `responses.text` and `responses.redirect` directly.
- `ClientDisconnected` — default handler calls `Writer.abort()` (no response body).

**CLI and server configuration**

- Configure the server with `STARIO_*` environment variables — see `stario serve --help`. Stario does not auto-load dotenv files.
- `stario serve` / `stario watch` take only the app spec; `stario watch` keeps `--watch` / `--watch-ignore`.
- CLI entry point: `stario.cli.main:main`.
- Telemetry: set `STARIO_TRACER` and optional `STARIO_TRACERS_*`; construct `JsonTracer`, `SqliteTracer`, etc. directly in library code.

**Markup**

- Import HTML/SVG from `stario.markup` (for example `from stario.markup import html as h`).
- Package root re-exports framework primitives only — import `responses`, `cookies`, `Request`, and `Router` from their modules.

**Datastar**

- `from stario.datastar import data, at, SSE, read_signals, ModuleScript` — attribute and action helpers on `data.*` and `at.*`.
- One `SSE(w)` per response — create it once, then call `sse.patch_elements()`, `sse.patch_signals()`, `sse.navigate()`, and so on so the stream owns `Writer` headers and `Content-Type: text/event-stream` for every event.
- Signal names and dotted paths use Python `snake_case`; fetch options use `None` to omit.

**Telemetry**

- `Tracer` — `create(..., parent=)` and `on_end(span)`; call methods on the span handle.
- `Span.step()` for child spans; `Span.new_trace()` for a detached root; `Span.link(name, span_id, …)` for cross-span references.
- `RecordingSpan`, `NoOpSpan`, `ProxySpan`, `RecordedEvent`, `RecordedLink`, `TelemetryStats` exported from `stario.telemetry`.
- Backend env vars: `STARIO_TRACERS_SQLITE*`, `STARIO_TRACERS_JSON*`.
- `tracer.stats()` → `TelemetryStats` for sink health counters.
- Event `body` — `str`, `BaseException`, or `None`; structured data goes in attributes.
- Finished spans ignore mutations after `end()`.
- Traceback formatting: `stario.telemetry.formatters`.

### Removed

- `Router.mount`, `Router.push_middleware`, `App.url_for`, route `name=`
- `HttpException.respond()`, bundled tracer `from_env()` classmethods
- `stario init`, packaged CLI templates, CLI runtime flags (`--host`, `--port`, `--tracer`, compression, limits, timeouts, `--loop`, `--unix-socket`)
- `stario.telemetry.tracebacks`
- Flat `from stario import datastar as ds` namespace and module-level `datastar.sse.*` helpers

### Added

- `ServerConfig` and `RequestPolicy` — listen, compression, shutdown, and request limits (`stario.http.config`).
- `AssetManifest`, `Asset`, and `StaticAssets.stats`.
- Static serving — `precompress=` codec selection, per-instance `content_types=` overrides, and `Range: bytes=…` on large streamed files (206 / 416; one range per request).
- `STARIO_REUSE_ADDR` — TCP `SO_REUSEADDR` (default `1`).
- `normalized_location` — shared redirect URL safety for `responses.redirect` and SSE navigation.

## 3.4.0 - 2026-05-27

- `STARIO_TRACER` and `from_env()` on bundled tracers — configure SQLite/JSON sinks from the environment without CLI flags.

## Earlier 3.x

- 3.3.0 — `NoOpTracer`, HTTP hot-path performance, HTML rendering internals.
- 3.2.0 — `Context.closing`, `App.wait_shutdown()`, Datastar v1.0.1 in examples.
- 3.1.0 — Datastar 1.0 compatibility, `Relay.subscribe(*patterns)`.
- 3.0.x — `App` replaces `Stario`, explicit bootstrap, `stario.datastar`, docs site at [stario.dev](https://stario.dev). See git tags for full 3.0.0 migration notes from 2.x.
