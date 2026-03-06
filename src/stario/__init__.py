"""
Stario - Real-time Hypermedia for Python.

Core:
    from stario import Stario, Router, Request, Writer

Types:
    from stario import Handler, Middleware, Context, UrlFor

Static files:
    from stario import StaticAssets

Pub/Sub:
    from stario import Relay

Telemetry:
    from stario import Tracer, Span, RichTracer, JsonTracer

Datastar (hypermedia):
    from stario import DatastarScript, at, data

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
from stario.datastar import DatastarScript as DatastarScript
from stario.datastar import at as at
from stario.datastar import data as data

# =============================================================================
# Exceptions
# =============================================================================
from stario.exceptions import ClientDisconnected as ClientDisconnected
from stario.exceptions import HttpException as HttpException
from stario.exceptions import SignalValidationError as SignalValidationError
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

# =============================================================================
# Types - Handler signatures
# =============================================================================
from stario.http.types import Context as Context
from stario.http.types import Handler as Handler
from stario.http.types import Middleware as Middleware
from stario.http.types import UrlFor as UrlFor
from stario.http.writer import CompressionConfig as CompressionConfig
from stario.http.writer import Writer as Writer

# =============================================================================
# Pub/Sub - In-process messaging
# =============================================================================
from stario.relay import Relay as Relay

# =============================================================================
# Telemetry - Tracing and observability
# =============================================================================
from stario.telemetry import JsonTracer as JsonTracer
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
    "UrlFor",
    # Static Files
    "StaticAssets",
    # Pub/Sub
    "Relay",
    # Telemetry
    "Tracer",
    "Span",
    "RichTracer",
    "JsonTracer",
    # Datastar
    "DatastarScript",
    "at",
    "data",
    # Exceptions
    "HttpException",
    "ClientDisconnected",
    "SignalValidationError",
    "StarioError",
    # Testing
    "TestRequest",
    "ResponseRecorder",
]
