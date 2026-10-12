"""
HTTP apps as explicit routes + plain HTML trees; wire protocol and rendering stay visible.

The root carries the names a handler file needs:

```python
from stario import App, Context, Route, Span, Writer, h
```

- **Per-handler:** `App`, `Context`, `Writer`, `Route`, `h`, `classes`
- **Control flow:** `RedirectException`, `catch_errors` middleware (`stario.http.middleware`)
- **Bootstrap / filesystem:** `Assets`, `Files`, `Span`
- **Process:** `serve` — async HTTP server; wrap with `asyncio.run` or `uvloop.run`; `ServerConfig`
- **Middleware / realtime:** `Handler`, `Middleware`, `Relay`
- **Types:** `JsonValue`

Feature areas stay in their modules: `stario.datastar` (`at`, `data`, `SSE`),
`stario.markup` (`baked`, `render`, `svg`), `stario.cookies`, and `stario.http`
(`Router`, `Headers`, `run_app`). Deep imports are always allowed.

Register endpoints on `App` with `app.add(Route("GET /"), home)`. Scope middleware
with `app.use("/api", mw)` on a path prefix.
"""

from importlib.metadata import version as _package_version

__version__ = _package_version("stario")

from stario.exceptions import RedirectException
from stario.filesystem import Assets, Files
from stario.http.app import App
from stario.http.config import ServerConfig
from stario.http.context import Context, Handler, Match, Middleware
from stario.http.route import Route
from stario.http.server import serve
from stario.http.writer import Writer
from stario.json import JsonValue
from stario.markup import classes
from stario.markup import html as h
from stario.relay import Relay
from stario.telemetry import Span

__all__ = [
    "App",
    "Assets",
    "Context",
    "Files",
    "Handler",
    "JsonValue",
    "Match",
    "Middleware",
    "RedirectException",
    "Relay",
    "Route",
    "ServerConfig",
    "Span",
    "Writer",
    "__version__",
    "classes",
    "h",
    "serve",
]
