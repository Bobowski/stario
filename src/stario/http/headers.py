"""HTTP header container used by the parser, writer, and application API.

`Headers` is the Cython pair list: each field is stored as wire-ready
`name: ` / `value\\r\\n`. `get` / `set` still see clean strings. Repeated
fields such as `Set-Cookie` stay as adjacent pairs.

Request headers on the Cython protocol stay an arena scan (`RequestHeaders`).
This type is the response map and the Python httptools path.

`encode_header_value` validates handler-supplied values; prefer `Headers.set`
in application code.
"""

from stario_cython.headers import Headers, encode_header_value

__all__ = ["Headers", "encode_header_value"]
