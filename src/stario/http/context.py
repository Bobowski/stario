"""
Request-scoped bundle for handlers: ``app``, ``req``, ``span``, routing match, and a shared ``state`` dict for middleware.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from stario.telemetry.core import Span

from .request import Request
from .writer import Writer

if TYPE_CHECKING:
    from .app import App


@dataclass(slots=True, frozen=True)
class RouteMatch:
    """Result of routing: a canonical pattern string plus captured path/host segments."""

    pattern: str
    """Matched route template (useful for logs), including host part when present."""
    params: Mapping[str, str]
    """Map from ``{param}`` / ``{rest...}`` names to decoded segment text."""


EMPTY_ROUTE_MATCH = RouteMatch(pattern="", params=MappingProxyType({}))


@dataclass(slots=True)
class Context:
    """Per-request bundle passed to every handler and middleware (routing fills ``route`` before the handler runs)."""

    app: "App"
    """The ``App`` instance for this request."""
    req: Request
    """Parsed HTTP request (method, path, headers, body reader)."""
    span: Span
    """Telemetry span for this request; started/ended by the app callable."""
    state: dict[str, Any] = field(default_factory=dict)
    """Mutable dict for middleware to pass data to inner layers and the handler."""
    route: RouteMatch = field(default=EMPTY_ROUTE_MATCH)
    """Filled when the app resolves the route, before your handler runs."""

type Handler = Callable[[Context, Writer], Awaitable[None]]

type Middleware = Callable[[Handler], Handler]
