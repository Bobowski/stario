"""HTTP stack for Stario apps and servers.

**Dispatch** — `Router` in `stario.http.dispatch` matches requests to handlers.
`App` subclasses it and adds error handling plus the protocol entrypoint.

**Message** — `Request`, `Writer`, `Headers`, `ParsedQuery` for one HTTP exchange.

**Process** — import submodules directly for embedding:

```python
from stario.http.bootstrap import bootstrap_run
from stario.http.compression import CompressionConfig
from stario.http.config import RequestPolicy, ServerConfig, server_config_from_env
from stario.http.redirect import normalized_location
from stario.http.server import Server
```

Route patterns and link building live in `stario.routing`, not here.
Static file serving lives in `stario.staticassets`.

For tests, `aload_app` is re-exported from `stario.testing`.
"""

from stario.http.app import App
from stario.http.context import Context, Handler, Middleware, RouteMatch
from stario.http.dispatch import Router, default_not_found, method_not_allowed_handler
from stario.http.headers import Headers
from stario.http.query import ParsedQuery
from stario.http.redirect import normalized_location
from stario.http.request import Request
from stario.http.writer import Writer

__all__ = [
    "App",
    "Context",
    "Handler",
    "Headers",
    "Middleware",
    "ParsedQuery",
    "Request",
    "RouteMatch",
    "Router",
    "Writer",
    "default_not_found",
    "method_not_allowed_handler",
    "normalized_location",
]
