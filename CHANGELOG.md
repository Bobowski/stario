# Changelog

All notable changes to Stario are documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## 3.2.0 - 2026-04-30

### Added

- **`Writer.closing`** — True when **`disconnected`** or **`shutting_down`** applies; convenient guard for loops that exit on explicit breakpoints instead of task cancellation alone.
- **`App.wait_shutdown()`** and **`App.shutting_down`** — Observe when the server (or equivalent test lifecycle) begins draining **`app.create_task`** work **without** a **`Writer`**. **`Server`** links the runtime shutdown future to **`App`**; **`stario.testing.TestClient`** (plain **`App`** or bootstrap) and **`aload_app`** complete that signal before **`drain_tasks`** / bootstrap teardown so background tasks unblock like production.

### Changed

- **`Writer.alive()`** — Implements the disconnect/shutdown waiter with **`asyncio.wait`**; exiting the manager only suppresses **`CancelledError`** when **`alive()`** requested cancellation (**not** when another caller cancelled the task).
- **Bundled Datastar** — Default **`ModuleScript()`** / **`DATASTAR_CDN_URL`** and vendored **`datastar.js`** in CLI **`hello-world`** and **`tiles`** templates and the **chat-room** example track **Datastar v1.0.1** (see [getting started](https://data-star.dev/guide/getting_started)).

## 3.1.0 - 2026-04-20

### Datastar 1.0 compatibility

Stario’s **`stario.datastar`** helpers, the default bundled script, and **`read_signals`** are aligned with upstream **Datastar 1.0** (attribute names, signal wire format, and the **`ModuleScript()`** pin).

- **`datastar.read_signals`** — **DELETE** requests now read signals from the **`datastar`** query parameter, like **GET**, instead of the body. This matches Datastar’s client after [PR #1146](https://github.com/starfederation/datastar/pull/1146) (signals are only sent in the body for methods other than GET and DELETE).
- **Bundled Datastar** — Default **`ModuleScript()`** / **`DATASTAR_CDN_URL`** and vendored **`datastar.js`** in CLI templates and the **chat-room** example track **Datastar v1.0.0** (was **v1.0.0-RC.8**).
- **Datastar Pro attribute helpers** — **`animate`**, **`custom_validity`**, **`match_media`**, **`on_raf`**, **`on_resize`**, **`persist`**, **`query_string`**, **`replace_url`**, **`scroll_into_view`**, **`view_transition`** (commercial Pro wire format; the open-source client ignores them unless Pro is enabled).
- **Attribute helpers** — **`bind`** accepts optional **`case`**, **`prop`**, and **`event`**; **`on_intersect`** accepts **`threshold`** and no longer takes **`half`** or **`viewtransition`**; **`on()`** emits compact **`data-on:`** keys when only the event name and expression are needed; docstrings link to **[data-star.dev](https://data-star.dev/reference)**.

### Relay

- **`Relay.subscribe`** — Accepts **one or more** patterns (**`*patterns`**); overlapping patterns are **deduplicated** to a minimal equivalent set. Calling **`subscribe()`** with **no** patterns raises **`TypeError`**.

## 3.0.1 - 2026-04-15

### Changed

- **`stario init`** — Runs **`uv add`** with **`stario>=N,<N+1`**, where **N** is the **major** version of the running CLI (for example **`stario>=3,<4`** for a 3.x CLI), so new projects stay on the same major line instead of resolving an unpinned **`stario`** that could still be **2.x** on an index.

### Examples

- **chat-room** — **`pyproject.toml`** declares **`stario>=3,<4`** to match the init pattern; README updated accordingly.

## 3.0.0 - 2026-04-15

### Highlights

- **Application entrypoint** — `bootstrap(app, span)` is the single supported way to register routes and run startup/shutdown (async context managers, single-yield async generators, awaitables, or plain `None` all normalize to one contract).
- **Routing** — Route patterns support named path segments (`{name}`, `{rest...}`) at registration time. Matched values appear in **`c.route.params`** on **`Context.route`**, and **`c.route.pattern`** records the template that won.
- **HTTP surface** — `Writer` stays a thin byte/stream primitive; **`stario.responses`** holds the higher-level helpers so “protocol” and “convenience” do not interleave.
- **Datastar** — Attributes, actions, signal reading, and SSE event writers live under **`stario.datastar`** (`from stario import datastar as ds`), separated from `Writer` so request/response wiring stays obvious.
- **Validation** — Parsing and validation of Datastar signals and other inputs are **yours**: Stario no longer bundles opinionated adapters for Pydantic, cattrs, or similar. See the docs on [reading and writing signals](https://stario.dev/docs/how-tos/reading-and-writing-signals) and [no validation layer in the framework](https://stario.dev/docs/explanation/no-validation-layer-in-the-framework).

### Breaking changes (migrate from 2.3)

- **`Stario` → `App`** — The concrete application type is now **`App`**. `Server` and CLI type aliases use **`AppBootstrap`** / **`AppFactory`** instead of `Stario*`.
- **`Context`, `Handler`, `Middleware` move** — They are defined in **`stario.http.context`**, not `stario.http.types` (that module is removed).
- **`UrlFor` type removed** — Reverse URLs use **`app.url_for(name, params=…, query=…)`** on **`App`** (`params` fills `{placeholders}` in the registered pattern; `query` builds `?…`).
- **Static files** — Mount with **`app.mount("/static", StaticAssets(…, name="static"))`**; **`StaticAssets`** now behaves as a router subtree with **`name=`** for **`url_for`** (older error-text examples wrongly said **`app.assets(…)`**—that API never existed).
- **Default CLI tracer** — **`RichTracer`** is removed. In a TTY, **`TTYTracer`** is the default (`--tracer auto|tty`); JSON and SQLite tracers remain.

### Added

- **`RedirectException`** — Redirects with a dedicated type (still integrates with the same error surface as **`HttpException`**).
- **`App.url_for()`** — Reverse routing by **`name`** with **`params`** for named segments and **`query`** for the query string.
- **Router middleware** — **`Router(middleware=[…])`** (including **`App`**, which subclasses **`Router`**) wraps handlers as they are registered on that router; **`push_middleware`** appends and re-wraps existing routes. Per-route **`middleware=`** on **`handle` / `get` / …** runs after the router’s stack (see **Routing** in the docs).
- **`App.join_tasks()`** — Await background work scheduled with **`App.create_task`** (primarily for tests after a response completes).
- **`RelaySubscription`** — Ergonomic **`async with`** / **`async for`** around **`Relay.subscribe`** with clearer registration teardown.
- **Request size limits** — **`Server`** accepts **`max_request_header_bytes`** and **`max_request_body_bytes`** (defaults reject oversize heads with **431** and bodies with **413**).
- **Telemetry** — **`stario.telemetry.tracebacks`** for shared traceback formatting; **`stario.telemetry.tty.TTYTracer`** replaces the old Rich-based TTY experience.

### Changed

- **Trailing slashes** — Requests whose path is not `/` but ends with `/` receive **308** to the normalized path (same rules as the router’s path normalization).
- **`StaticAssets`** — Mounts as a router subtree; supports **`name=`** for **`url_for`**, configurable hashing chunk size and compression parameters, and clearer non-fingerprint → fingerprint redirects.
- **`Relay`** — Safer cross-thread **`publish`**, cleaner handling when a subscriber’s loop has shut down.
- **Unix domain socket bind** — If the socket path already exists and is **not** a socket, startup fails with a clear error instead of unlinking arbitrary files.
- **Root package exports** — **`stario`** now re-exports **`cookies`**, **`html`**, **`responses`**, **`svg`**, **`HttpException`**, **`RedirectException`**, **`StaticAssets`**, **`RelaySubscription`**, and documents **`from stario import datastar as ds`**.
- **Datastar `read_signals`** — Documentation stresses untrusted input; extra attribute helpers re-exported (**`attrs`**, **`classes`**, **`computeds`**, **`signal`**, **`styles`**).
- **`HttpException.respond`** — Lazily imports **`stario.responses`** to keep import graphs light.
- **`TestClient` / `aload_app`** — Reworked for **`App`**, streaming responses, cookie jars, and telemetry-focused tests (see `stario.testing` and the test suite).

### Removed

- **`rich`** and **`click`** from runtime/CLI dependencies — The **`stario`** CLI uses **`argparse`** and **`stario.cli.term`** instead.
- **`stario.telemetry.rich`** — Superseded by **`TTYTracer`** and traceback helpers.

### CLI and dependencies

- **TTY tracer** — Default interactive tracer output uses **`TTYTracer`** instead of **`RichTracer`**.

### Documentation site ([stario.dev](https://stario.dev))

Released in lockstep with the framework: docs **reorganised and rewritten** for current APIs, a **table of contents** on documentation pages (“On this page”), and **full-text search** from the navbar over the doc set.

### Notes for upgrades from 2.x

Treat this as a **major** release: follow the docs for renamed modules and behavioural changes, port in small steps, and open an issue when anything is unclear—that usually means the docs need a line, not that you are wrong.
