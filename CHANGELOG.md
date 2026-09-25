# Changelog

All notable changes to Stario are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 5.0.0 - Unreleased

### Breaking changes

- Cython llhttp/nghttp2 is the production HTTP runtime. The Python httptools
  protocol is gone. `stario serve` uses the compiled protocol.
- **Stario is a compiled package.** PyPI ships wheels for Linux (x86_64,
  aarch64; glibc) and macOS (arm64) on CPython 3.12–3.14 and 3.14t, with
  nghttp2 and Brotli bundled. Other platforms build from the sdist and need
  a C compiler, `pkg-config`, and the nghttp2 (1.61+) and Brotli development
  packages. Windows and musl are not supported.
- `stario_cython.request` is gone; import `Request` from `stario.http.request`
  (typing) or `stario_cython.exchange`.
- `Headers`, `ParsedQuery`, and `ParsedCookies` are typed as the concrete
  Cython classes (`stario_cython/*.pyi`), so `Headers()` / `ParsedQuery(b"")`
  type-check. `Request` stays a structural `Protocol` that `TestRequest`
  satisfies.
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
- **`c` / `w` belong to one handler call.** Each dispatch gets a fresh handle
  (one object is both `c` and `w`). Once the handler has returned and the
  response is complete, the handle is finished: `respond()`, `write()`,
  `write_headers()`, and `w.headers` raise `StarioRuntime`; `end()` /
  `abort()` are no-ops; `req`, `match`, `state`, `span`, and `status_code`
  still describe that request. A `c.req` (or its headers, query, cookies)
  kept past the handler keeps its own data. Its body is still readable if the
  handler read it, otherwise `body()` raises `StarioRuntime`.
- **Paths follow RFC 3986 / RFC 9112.** Routing splits the raw path on `/`
  and percent-decodes each segment, so `%2F` is data inside one segment and
  params are fully decoded (`/files/a%2Fb` → `name="a/b"`, `%252F` →
  `"%2F"`). `req.path` is the fully decoded path and the new `req.raw_path`
  is the path as sent. `find_handler(host, path, method)` takes the raw path.
  Dot segments (`.`, `..`, `%2E` forms) 308 to the normalized path, like a
  trailing slash. 400 (in order, keep-alive preserved, HTTP/2 stream only)
  for a malformed `%XX`, invalid UTF-8, a decoded control byte, a `#`, or
  `*` with any method but `OPTIONS`. `OPTIONS *` answers 204. HTTP/1
  absolute-form (`GET http://host/path`) routes on its path and uses the URI
  authority as the host. Route patterns reject `.` / `..` segments.
- 204 and 304 responses no longer send `Content-Length` (or `Content-Type`).
  `respond()` / `write_headers()` reject statuses outside 200–599.
- **Pipelined errors keep response order.** A protocol error (bad request,
  429 over the pipeline cap) behind in-flight pipelined requests is written
  only after their responses, then the connection closes. Keep-alive 413/431
  are answered in order like any response. Previously the error could be
  read as the answer to an earlier request whose handler still ran.
- **Upload backpressure before the body is read.** If a handler has started
  but not asked for the body, HTTP/1 reading pauses after 64 KiB instead of
  buffering up to `max_body_bytes`; the declared `Content-Length` is only
  reserved once `body()` reads it. HTTP/2 holds that stream's WINDOW_UPDATE
  instead of pausing the socket, so one slow consumer no longer stalls
  other streams.
- **HTTP/2 graceful drain.** Shutdown sends GOAWAY with the last processed
  stream; in-flight streams finish, new ones are refused, and the connection
  closes when nghttp2 is done (also after a client GOAWAY).
- Dynamic responses compress with `br` or `gzip` only. `zstd` is still
  served for precompressed `Files` / `Assets` variants; the `zstd_*`
  `CompressionConfig` fields apply only there.
- Idle keep-alive connections hold ~8 KiB instead of ~69 KiB (the read
  buffer is no longer zero-filled).
- Each header field counts 32 bytes toward `max_header_bytes` (RFC 7541
  §4.1), bounding the field count. `Host: example.com.` matches
  `example.com`. A failure while starting a handler (e.g. a tracer error)
  answers 500 instead of stalling the connection.

### Added

- `await w.drain()` — write-side backpressure for streaming handlers.
  `write()` never blocks; `drain()` waits while the transport has paused
  writing or an HTTP/2 stream has more than 256 KiB of unsent DATA, and
  returns immediately once the client is gone or the request has finished.
  `Files` / `Assets` await it after each chunk, so a slow client no longer
  makes the server read a whole file into memory. TestClient's writer and
  the `Writer` protocol have it too.

- `STARIO_THREADS` — opt-in worker count (`1` default). `N>1` runs N
  event-loop threads, each a full `create_server` on the same TCP port
  via `SO_REUSEPORT` (thread 0 also owns signals and shutdown). The
  kernel load-balances new connections. `STARIO_LOOP` is resolved once
  and audited per thread. If `SO_REUSEPORT` cannot share the listen
  address (Unix sockets on many kernels, Windows), Stario stays at one
  thread. Requires free-threaded Python 3.14t unless
  `STARIO_THREADS_ALLOW_GIL=1`. See `docs/free-threading.md`.
- `stario.http.middleware.catch_errors` — wrap handlers so listed exceptions
  become HTTP responses when nothing was sent yet. Presets:
  `catch_request_body_errors()` and `respond_request_body_error`.
- Cython HTTP/2 via nghttp2 on the same connection class. Switch once per
  socket (TLS ALPN `h2` or the cleartext connection preface). Responses go
  out as frames, not HTTP/1.1 text.
- Direct TLS: `ServerConfig(ssl=…)` or `STARIO_SSL_CERTFILE` /
  `STARIO_SSL_KEYFILE`. Context is TLS 1.2+ with ALPN `h2`, `http/1.1`.

### Fixed

- HEAD requests with `Accept-Encoding` no longer get a compressed body
  after the headers (which desynced keep-alive).
- `write()` past a declared `Content-Length` raises `StarioRuntime` before
  sending anything; previously the extra bytes went out and corrupted the
  next response on the connection.
- HTTP/1 chunked trailers are dropped (RFC 9110 §6.5) instead of being
  merged into `c.req.headers` after the handler started.
- HTTP/2 copies a caller's `bytearray` passed to `write()` / `respond()`;
  mutating it afterwards no longer changes what is sent.
- HTTP/2 `end()` no longer cancels a handler that keeps running after it,
  and a client `RST_STREAM` is not echoed back.
- A pipelined request trickling its headers now hits the header timeout
  after the earlier response finishes (it used to get no deadline).
- HTTP/1.0 requests with `Transfer-Encoding` close after the response
  (RFC 9112 §6.1).
- `Headers.unsafe_*` raise `TypeError` for non-`bytes` arguments instead of
  reading invalid memory. `memoryview` bodies must be flat, contiguous
  bytes (`itemsize` 1, one dimension); others raise `TypeError` before
  anything is sent.
- Streaming to an HTTP/1.0 client without `Content-Length` sends a
  close-delimited body with `Connection: close` instead of chunked framing
  the client cannot parse.
- Chunked and gzip writes accept `memoryview` parts, and gzip handles
  inputs over 4 GiB.
- HTTP/2: client PINGs no longer keep an idle connection open; a stream
  whose response is done but whose request body is still open is reset
  (`NO_ERROR`) once the handler finishes; paused streams no longer let the
  connection window grow past its limit; fatal protocol errors flush the
  GOAWAY before closing; exceptions in header/data callbacks reset the
  stream instead of being ignored.
- HTTP/2 drain is two-phase: a shutdown-notice GOAWAY and PING, then the
  final GOAWAY on the PING ACK (or after 1 s), so requests in flight are
  not refused.
- HTTP/1: bare CRLFs between requests no longer keep an idle connection
  open.
- A body `stream()` or `body()` call that outlives its request raises
  `StarioRuntime` instead of reading the next request's body on the
  pooled exchange.
- Failures swallowed inside the server (nghttp2 callbacks, dispatch,
  exchange allocation, tracer spans, protocol error responses) are logged
  with tracebacks.

### Changed

- `find_handler` walks a compiled trie with no cache in front. A static
  route returns a prebuilt `(handler, route, Match)`; a parameterized route
  allocates one `Match`. Exact hosts have their own path trie. One cursor
  walks the trie, one segment per node, so route insertion order never
  changes a match.
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
- Cython request headers are a standalone read-only arena view (no unused
  `Headers` pair list). Timeout cleanup is chosen in `HttpProtocol.__init__`
  (`timeout_cleanup=`, env as default). Cookie `as_dict()` is cached. H1/H2
  methods share one byte table. Compressibility uses the Python helper.
- `stario.http.host.host_without_port` re-exports the Cython Host parser
  so request routing and the helper agree.

## 4.3.0 - 2026-09-25

### Added

- `stario.serve(bootstrap, …)` — run the HTTP server on a loop you start:
  `asyncio.run(stario.serve(bootstrap, port=9000))` or `uvloop.run(...)`.
  Listen settings are keywords (`host`, `port`, `unix_socket`, …) or a
  prepared `config=`. After shutdown the coroutine finishes and the caller
  can continue. Omit `tracer` to get a TTY or JSON tracer for the call.

### Changed

- Python 3.12 and 3.13 are supported. The package requires Python 3.12 or newer.
- zstd uses the `zstandard` package on every Python. Stario no longer imports
  stdlib `compression.zstd`.
- Span and trace ids use the CPython 3.14 UUIDv7 bit layout
  (RFC 9562 Method 1) in `RecordingSpan.create`.
- `Server` raises if `unix_socket` is set and the platform has no `AF_UNIX`.
  `stario serve` / `stario watch` no longer check this before start.
- Startup span `server.event_loop` is the loop that is running, not
  `ServerConfig.event_loop`.

## 4.2.0 - 2026-09-21

### Added

- `Assets` and `Files` — a directory at a URL prefix. Call `href()` at import.
  Call `await attach(app)` in bootstrap to register GET/HEAD and load the tree.
  `register()` and `load()` stay available. `Assets` hashes names and 307s the
  logical path. Both send strong ETags, 304, and `nosniff`. Pass
  `content_types=` to add or override MIME types.
- `stario.json` — one process-wide codec for JSON responses, Datastar signals,
  telemetry, and the test client. Replace the standard-library default with
  `set_codec()`.

### Changed

- `Route` is the HTTP address: `Route("GET /home")` or
  `Route("POST", ROOM + "/send")`. After a match, read `c.match`. Matching
  lives in `stario.http`.
- Static routes hit an exact map. Parameterized routes walk a compressed trie.
  Pass `Request.host` (already folded). The matcher does not lowercase host.
- File streaming uses `os.pread` in a worker thread. The `aiofiles`
  dependency is gone.

### Fixed

- TTY tracer live footer — skip the write when text and width do not change.
  Cap the live block so erase cannot clear scrollback.
- `Assets.load()` re-hashes files already pinned by `href()`, so a same-size
  content edit is not missed.

### Deprecated

These still work. They will be removed in 5.0.

- `UrlPath` — prefer `Route` or a `/` / `//` string. `Files` and `Assets`
  take strings.
- `app.get` / `app.post` / `app.handle` and `Route.get` / `Route.post` / …
  — use `app.add(Route("GET /"), handler)`. `Route.query(path)` stays until
  5.0; `Route("QUERY /feed")` is the replacement.
- `at.fetch` — build the URL with `route.href()` and name the verb at the
  call site (`at.get(...)`, `at.post(...)`).
- `stario.staticassets` (`AssetManifest`, `StaticAssets`) — use `Assets` or
  `Files` and `await attach(app)`.

### Removed

- `c.route` (`RouteMatch`) — use `c.match` (`Match`).
- `stario.routing` — import `Route` and `UrlPath` from `stario` or
  `stario.http`.

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
- `AssetManifest` and `StaticAssets.stats`.
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
