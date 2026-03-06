"""
Datastar Integration - Hypermedia for modern frontends.

Datastar (https://data-star.dev) is a lightweight framework for building
reactive UIs using SSE (Server-Sent Events) and declarative HTML attributes.

Why Datastar instead of htmx/Alpine/React?
- SSE is simpler than WebSockets (unidirectional, auto-reconnect)
- Declarative HTML means no build step
- Signals provide reactive state without a virtual DOM
- ~14KB gzipped for the entire frontend

How it works:
1. Frontend sends request (GET/POST)
2. Backend streams SSE events (patch-elements, patch-signals)
3. Datastar applies patches to the DOM

Security notes:
- CSRF: Datastar sends signals as JSON body, so standard SameSite=Lax cookies
  provide CSRF protection for state-changing operations. For additional safety,
  verify Origin/Referer headers in sensitive endpoints.
- XSS: All text content is escaped by default. Use SafeString only for trusted
  HTML that you control (never user input).

This module provides:
- SSE formatters: sse.patch(), sse.signals(), sse.redirect(), sse.script()
- HTML helpers: DatastarScript()
- Attribute helpers: data.signals(), data.on(), data.bind()
- Action helpers: at.get(), at.post()
- Signal parsing: parse_signals(), r.signals()
"""

from stario.html import HtmlElement, Script

# SSE event formatters (simple functions → bytes)
from . import sse as sse
from .actions import DatastarActions as DatastarActions
from .attributes import DatastarAttributes as DatastarAttributes

# JS expression builders
from .format import js as js
from .format import s as s

# Signals parsing
from .parse import FileSignal as FileSignal
from .parse import parse_signals as parse_signals

DATASTAR_CDN_URL = (
    "https://cdn.jsdelivr.net/gh/starfederation/datastar@1.0.0-RC.8/bundles/datastar.js"
)


def DatastarScript() -> HtmlElement:
    """Return the standard Datastar module script tag."""
    return Script({"type": "module", "src": DATASTAR_CDN_URL})


# Attribute and action builders (singletons)
data = DatastarAttributes()
at = DatastarActions()
