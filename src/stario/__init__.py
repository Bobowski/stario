"""
HTTP apps as explicit routes + plain HTML trees; wire protocol and rendering stay visible (no magic request globals).

The package root re-exports the types most handlers touch; use submodules (``stario.responses``, ``stario.datastar``, …) for helpers.

Prefer ``from stario import …`` by name—for markup, ``from stario import html as h`` and ``from stario import svg`` are the usual entry points (both are the same modules as ``stario.html`` and ``stario.html.svg``). Import tag helpers explicitly (for example ``from stario.html import Div, P``) or use the submodule ``from stario.html import tags as tags`` when you want the full catalog without a long import list. **Datastar** helpers use ``from stario import datastar as ds`` (the ``stario.datastar`` package). **Intentional HTTP outcomes** use ``HttpException`` for bodies (4xx/5xx) and ``RedirectException`` for redirects (3xx), both from ``stario.exceptions`` and re-exported here.
"""

from importlib.metadata import version as _package_version

__version__ = _package_version("stario")

from stario import cookies as cookies
from stario import html as html
from stario import responses as responses
from stario.exceptions import HttpException as HttpException
from stario.exceptions import RedirectException as RedirectException
from stario.html import svg as svg
from stario.http.app import App as App
from stario.http.context import Context as Context
from stario.http.context import Handler as Handler
from stario.http.context import Middleware as Middleware
from stario.http.request import Request as Request
from stario.http.router import Router as Router
from stario.http.staticassets import StaticAssets as StaticAssets
from stario.http.writer import Writer as Writer
from stario.relay import Relay as Relay
from stario.telemetry import Span as Span
