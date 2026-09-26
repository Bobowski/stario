"""Header types. The implementations live in ``stario_cython.exchange``.

``Headers`` is the mutable response header list (``w.headers``).
``RequestHeaders`` is the read-only view handlers get as ``c.req.headers``.
"""

from stario_cython.exchange import Headers, RequestHeaders, encode_header_value

__all__ = ["Headers", "RequestHeaders", "encode_header_value"]
