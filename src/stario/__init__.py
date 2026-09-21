"""
HTTP apps as explicit routes + plain HTML trees; wire protocol and rendering stay visible.

**Package layout**

- `stario` (this module) — daily handler primitives re-exported below.
- `stario.http` — routes, matching, request/response wire, `Router`, and server embedding.
- `stario.json` — process-wide JSON codec used by framework JSON operations.
- `stario.filesystem` — a directory as HTTP URLs (`Assets`, `Files`).
- `stario.staticassets` — obsolete fingerprinted assets (`AssetManifest`, `StaticAssets`).
- `stario.responses` / `stario.cookies` — thin helpers on `Writer`.

Import feature areas from their modules: `import stario.responses as responses`,
`from stario.datastar import at, data`, and symbols from `stario.markup`
(for example `from stario.markup import baked` and `from stario.markup import html as h`).

Prefer `from stario import …` for:

- **Per-handler:** `App`, `Context`, `Writer`, `Route`
- **Control flow:** `HttpException`, `RedirectException`
- **Bootstrap / filesystem:** `Assets`, `Files`, `Span`
- **Middleware / realtime:** `Handler`, `Middleware`, `Relay`

Register endpoints on `App` with `app.add(Route("GET /"), home)`. Scope middleware
with `app.use("/api", mw)` on a path prefix.
Import `Router` from `stario.http` when you need a separate route table. For HTTP types
(`Request`, `ParsedQuery`, `Headers`), use `stario.http`.
"""

from importlib.metadata import version as _package_version

__version__ = _package_version("stario")

from stario.exceptions import HttpException, RedirectException
from stario.filesystem import Assets, Files
from stario.http.app import App
from stario.http.context import Context, Handler, Match, Middleware
from stario.http.route import Route, UrlPath
from stario.http.writer import Writer
from stario.relay import Relay
from stario.staticassets import (
    AssetManifest,  # pyright: ignore[reportDeprecated]
    StaticAssets,  # pyright: ignore[reportDeprecated]
)
from stario.telemetry import Span

__all__ = [
    "App",
    "AssetManifest",
    "Assets",
    "Context",
    "Files",
    "Handler",
    "HttpException",
    "Match",
    "Middleware",
    "RedirectException",
    "Relay",
    "Route",
    "Span",
    "StaticAssets",
    "UrlPath",
    "Writer",
    "__version__",
]
