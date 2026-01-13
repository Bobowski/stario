"""
Stario - Real-time Hypermedia for Python.

Core:
    from stario import Stario, Router, Request, Writer

Types:
    from stario import Handler, Middleware, Context

Static files:
    from stario import StaticAssets, asset

Pub/Sub:
    from stario import Relay

Telemetry:
    from stario import Tracer, Span, Event, Link, RichTracer, JsonTracer

Datastar (hypermedia):
    from stario import at, data

HTML (separate module):
    from stario.html import Div, Span, render
"""

from importlib.metadata import version

__version__ = version("stario")

# =============================================================================
# Core - App and routing
# =============================================================================
# =============================================================================
# Datastar - Hypermedia helpers
# =============================================================================
from stario.datastar import at as at
from stario.datastar import data as data

# =============================================================================
# Exceptions
# =============================================================================
from stario.exceptions import ClientDisconnected as ClientDisconnected
from stario.exceptions import HttpException as HttpException
from stario.exceptions import StarioError as StarioError
from stario.http.app import Stario as Stario

# =============================================================================
# Request/Response - Handler parameters
# =============================================================================
from stario.http.request import Request as Request
from stario.http.router import Router as Router

# =============================================================================
# Static Files - Fingerprinted asset serving
# =============================================================================
from stario.http.staticassets import StaticAssets as StaticAssets
from stario.http.staticassets import asset as asset

# =============================================================================
# Types - Handler signatures
# =============================================================================
from stario.http.types import Context as Context
from stario.http.types import Handler as Handler
from stario.http.types import Middleware as Middleware
from stario.http.writer import CompressionConfig as CompressionConfig
from stario.http.writer import Writer as Writer

# =============================================================================
# Pub/Sub - In-process messaging
# =============================================================================
from stario.relay import Relay as Relay

# =============================================================================
# Telemetry - Tracing and observability
# =============================================================================
from stario.telemetry import Event as Event
from stario.telemetry import JsonTracer as JsonTracer
from stario.telemetry import Link as Link
from stario.telemetry import RichTracer as RichTracer
from stario.telemetry import Span as Span
from stario.telemetry import Tracer as Tracer

# =============================================================================
# Testing
# =============================================================================
from stario.testing import ResponseRecorder as ResponseRecorder
from stario.testing import TestRequest as TestRequest

# =============================================================================
# __all__ - Public API
# =============================================================================
__all__ = [
    # Core
    "Stario",
    "Router",
    # Request/Response
    "Request",
    "Writer",
    "CompressionConfig",
    "Context",
    # Types
    "Handler",
    "Middleware",
    # Static Files
    "StaticAssets",
    "asset",
    # Pub/Sub
    "Relay",
    # Telemetry
    "Tracer",
    "Span",
    "Event",
    "Link",
    "RichTracer",
    "JsonTracer",
    # Datastar
    "at",
    "data",
    # Exceptions
    "HttpException",
    "ClientDisconnected",
    "StarioError",
    # Testing
    "TestRequest",
    "ResponseRecorder",
]
