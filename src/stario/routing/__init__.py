"""Compile-time URL language: patterns, segments, and path normalization.

Public exports (submodules are also fine for narrower imports):

- ``UrlPath`` — route templates for registration and ``href()``
- ``normalize_path`` — canonical path strings
- ``append_query_fragment`` — query/fragment helper for asset hrefs and similar
- ``Segment`` — parsed pattern segment (trie matching and advanced tooling)

Read bottom-up when exploring internals: ``locations`` → ``segment`` → ``urlpath``.

No HTTP wire imports — safe for routes modules, templates, and asset manifests.
For request matching and handler dispatch, use ``stario.http.dispatch.Router``.
"""

from stario.routing.locations import append_query_fragment, normalize_path
from stario.routing.segment import Segment
from stario.routing.urlpath import UrlPath

__all__ = [
    "Segment",
    "UrlPath",
    "append_query_fragment",
    "normalize_path",
]
