"""Request type and body-size / timeout defaults.

``Request`` and ``ParsedCookies`` are Cython types from
``stario_cython.exchange``. Host parsing lives in ``stario.http.host``.
"""

from stario.http.host import host_without_port
from stario_cython.exchange import ParsedCookies, Request

# =============================================================================
# Security limits (used by ServerConfig / RequestPolicy)
# =============================================================================
DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
DEFAULT_MAX_HEADER_BYTES = 64 * 1024  # 64 KiB
DEFAULT_BODY_TIMEOUT = 30.0  # seconds

__all__ = [
    "DEFAULT_BODY_TIMEOUT",
    "DEFAULT_MAX_BODY_SIZE",
    "DEFAULT_MAX_HEADER_BYTES",
    "ParsedCookies",
    "Request",
    "host_without_port",
]
