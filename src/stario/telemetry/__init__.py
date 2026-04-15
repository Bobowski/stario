"""
Tracer implementations export different sinks; handlers only see ``Span`` attached to ``Context``.

Swap ``TTYTracer`` vs ``JsonTracer`` (or SQLite) at process startup without changing handler or protocol code.
"""

from .core import Span, Tracer
from .json import JsonTracer
from .sqlite import SqliteTracer
from .tty import TTYTracer

__all__ = ["Span", "Tracer", "JsonTracer", "SqliteTracer", "TTYTracer"]
