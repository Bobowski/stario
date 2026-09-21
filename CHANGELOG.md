# Changelog

All notable changes to Stario are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Breaking changes

- Cython llhttp/nghttp2 is the production HTTP runtime. The Python httptools
  protocol is gone. `stario serve` uses the compiled protocol.
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
- `c.route` (`RouteMatch`) is gone. Use `c.match` (`Match`).
- `stario.routing` is gone. Import `Route` and `UrlPath` from `stario` or
  `stario.http`.

### Fixed

- `Assets.load()` re-hashes files already pinned by `href()`, so same-size
  content changes are detected even when mtime does not move.
- TTY tracer live footer — skip terminal writes when the text and width do not
  change. Cap the live block to `terminal_rows - 2` so cursor-up erase cannot
  clear scrollback. Tall trees keep the root header and the newest lines.
  The footer is removed when no roots are open.

### Added

- `STARIO_THREADS` — opt-in worker count (`1` default). `N>1` runs one
  asyncio loop per OS thread. `STARIO_LOOP` (asyncio/uvloop) is resolved
  once: asyncio workers always get a stdlib loop (a process-wide
  `uvloop.install()` cannot leak in), uvloop workers call `uvloop.run`,
  and each worker refuses to start if the running loop is the other
  library. Requires free-threaded Python 3.14t unless
  `STARIO_THREADS_ALLOW_GIL=1`. See `docs/free-threading.md`.
- `Assets` and `Files` — one filesystem root plus a URL prefix. `href()`
  is lazy. `await attach(app)` registers GET/HEAD and loads the tree
  (`register()` and `load()` stay available). A later `attach()` on a
  new `App` re-adds GET/HEAD and reuses the loaded tree. `load()` still
  runs once. `Assets` hashes names and 307s logical paths. Both send
  strong ETags, 304, and `X-Content-Type-Options: nosniff`. Pass
  `content_types=` at construction to add or override MIME types.
- `stario.json` — one process-wide codec for JSON responses, Datastar signals,
  telemetry, and the test client. `dumps()` and `dumps_bytes()` preserve fast
  text and byte paths; `loads()` accepts text, bytes, and byte arrays. The
  standard-library default emits strict compact JSON. Replace it explicitly
  with `set_codec()`.
- `stario.http.middleware.catch_errors` — wrap handlers so listed exceptions
  become HTTP responses when nothing was sent yet. Presets:
  `catch_request_body_errors()` and `respond_request_body_error`.
- Cython HTTP/2 via nghttp2 on the same connection class. Switch once per socket (TLS ALPN `h2` or the cleartext connection preface). Responses go out as frames, not HTTP/1.1 text.
- Direct TLS: `ServerConfig(ssl=…)` or `STARIO_SSL_CERTFILE` / `STARIO_SSL_KEYFILE`. Context is TLS 1.2+ with ALPN `h2`, `http/1.1`.

### Changed

- `find_handler` keeps a 1024-entry LRU in front of the trie (same cap as
  pre-4.2 Cython). Static `(host, path, method)` still hits an exact map
  and reuses that `Match`. Parameterized paths reuse the resolved
  `(handler, route, Match)` while they stay in the cache. Exact hosts
  have their own path trie. Exact-only path chains are radix-compressed.
  One cursor walks the trie on a miss. The matcher does not lowercase
  `host` — pass `Request.host` (already folded).
- File streaming (`Assets`, `Files`, and `stario.staticassets`) reads
  already-open file descriptors with `os.pread` in a worker thread.
  The `aiofiles` dependency is gone.
- Handler-task finish is `stario.http.invoke.on_handler_done`: log, write 500
  if nothing was sent, abort if a body was started but not finished, close
  the span. No auto-`end()`. A write-then-raise still logs (`Handler failed`);
  the response already on the wire is not rewritten.
- Every request that writes an HTTP status gets a started-and-ended span:
  handler responses, trailing-slash 308, and protocol 400 / 413 / 431 / 429
  / 503. Protocol outcomes are not `fail`ed. `NoOpSpan` still skips start/end.
  Matched routes rename the span to `Route.pattern` and set `http.route`.
- Cython GET path: skip upload state when there is no body (`mark_nobody`),
  and arm idle timeouts on the Date-tick sweeper instead of `loop.time()`
  per keep-alive request.
- Cython uploads: Content-Length bodies ≤ 256 KiB dispatch after the
  message is complete (`body()` is already bytes). `stream()` with a known
  Content-Length yields `min(length, 256 KiB)` instead of a fixed 64 KiB.
- HTTP/2 POST without Content-Length is END_STREAM-delimited (like H1
  chunked). Recycle clears nghttp2 stream user_data. Duplicate `:method` /
  `:path` / `:authority` are RST; `:authority` and `Host` must be equal.
- `ParsedQuery` first read fills a C name/value span table. HTTP/1 stays on
  [llhttp](https://github.com/nodejs/llhttp).
- HTTP/2 receive window is 1MiB per stream / 4MiB per connection. RST-stream
  flood is rate-limited. Header budget 431 / body 413 apply per stream.

### Deprecated

These still work. They will be removed in 5.0.

Prefer `Route("GET /home")` or `Route("POST", ROOM + "/send")` and
`app.add(route, handler)`. Host is `//host/path` or `host=`. Paths
start with `/` or `//`. `{name}` and `{name...}` must be a whole path
segment or host label. `{{name}}` is a literal `{name}`. Query and
fragment go to `href()` only. After a match, read `c.match`.

- `UrlPath` — prefer `Route` or a `/` / `//` string. `href()` and `/`
  composition still work. `app.use`, `not_found`, `Files`, and `Assets`
  take strings.
- `app.get` / `app.post` / `app.handle` and `Route.get` / `Route.post`
  / … — register with `app.add(Route("GET /"), handler)`.
  `Route.query(path)` stays as the HTTP QUERY factory until 5.0;
  `Route("QUERY /feed")` is the replacement.
- `at.fetch` — build the URL with `route.href()` and name the verb at
  the call site (`at.get(...)`, `at.post(...)`).
- `stario.staticassets` (`AssetManifest`, `StaticAssets`) — use
  `Assets(...)` or `Files(...)` and `await attach(app)`.

## 4.1.1 - 2026-08-31

### Fixed

- `data.bind()` with `prop` or `event` — emit a key-only attribute. Datastar bind is exclusive (signal in the key or the value, not both); the previous form also set the value and Datastar raised `KeyAndValueProvided`.
- `data.persist(session=True)` — emit `data-persist__session` when `storage_key` is omitted.

### Added

- `data.on_intersect(..., view_transition=True)` — Datastar `__viewtransition` modifier.

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
