"""
HTTP apps as explicit routes + plain HTML trees; wire protocol and rendering stay visible.

**Package layout**

- `stario` (this module) — daily handler primitives re-exported below.
- `stario.routing` — compile-time URL language (`UrlPath`, `normalize_path`, …).
- `stario.http` — request/response wire, dispatch (`Router`), and server embedding.
- `stario.staticassets` — fingerprinted static files (`AssetManifest`, `StaticAssets`).
- `stario.responses` / `stario.cookies` — thin helpers on `Writer`.

Import feature areas from their modules: `import stario.responses as responses`,
`from stario.datastar import at, data`, and symbols from `stario.markup`
(for example `from stario.markup import baked` and `from stario.markup import html as h`).

Prefer `from stario import …` for:

- **Per-handler:** `App`, `Context`, `Writer`, `UrlPath`
- **Control flow:** `HttpException`, `RedirectException`
- **Bootstrap / assets:** `AssetManifest`, `StaticAssets`, `Span`
- **Middleware / realtime:** `Handler`, `Middleware`, `Relay`

Register routes on `App` (`app.get(HOME, …)`), scope middleware with `app.use(pattern, mw)`.
Import `Router` from `stario.http` when you need a separate route table. For HTTP types
(`Request`, `ParsedQuery`, `Headers`, `RouteMatch`), use `stario.http`.
"""

from importlib.metadata import version as _package_version

__version__ = _package_version("stario")

from stario.exceptions import HttpException, RedirectException
from stario.http.app import App
from stario.http.context import Context, Handler, Middleware
from stario.http.writer import Writer
from stario.relay import Relay
from stario.routing import UrlPath
from stario.staticassets import AssetManifest, StaticAssets
from stario.telemetry import Span

__all__ = [
    "App",
    "AssetManifest",
    "Context",
    "Handler",
    "HttpException",
    "Middleware",
    "RedirectException",
    "Relay",
    "Span",
    "StaticAssets",
    "UrlPath",
    "Writer",
    "__version__",
]
