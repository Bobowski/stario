"""HTTP stack for Stario apps and servers.

**Address and table** — `Route`, `Match`, and `Router`. `App` subclasses
`Router` and adds error handling plus the protocol entrypoint.

**Message** — `Request`, `Writer`, `Headers`, `ParsedQuery` for one HTTP exchange.

**Process** — import submodules directly for embedding:

```python
from stario.http.bootstrap import bootstrap_run
from stario.http.compression import CompressionConfig
from stario.http.config import RequestPolicy, ServerConfig, server_config_from_env
from stario.http.redirect import normalized_location
from stario.http.server import Server
```

Filesystem serving lives in `stario.filesystem` (`Files`).
For tests, import `aload_app` from `stario.testing`.
"""

from stario.http.app import App
from stario.http.context import Context, Handler, Match, Middleware
from stario.http.dispatch import Router, default_not_found, method_not_allowed_handler
from stario.http.headers import Headers
from stario.http.query import ParsedQuery
from stario.http.redirect import normalized_location
from stario.http.request import Request
from stario.http.route import Route, UrlPath
from stario.http.writer import Writer

__all__ = [
    "App",
    "Context",
    "Handler",
    "Headers",
    "Match",
    "Middleware",
    "ParsedQuery",
    "Request",
    "Route",
    "Router",
    "UrlPath",
    "Writer",
    "default_not_found",
    "method_not_allowed_handler",
    "normalized_location",
]
