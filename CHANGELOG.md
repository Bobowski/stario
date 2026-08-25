# Changelog

All notable changes to Stario are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- `STARIO_REUSE_PORT` — TCP `SO_REUSEPORT` so several Stario processes can
  share one listen port. Off by default. Set `1` and start multiple
  `stario serve` (or `python -m stario_cython`) processes on the same host/port.

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
