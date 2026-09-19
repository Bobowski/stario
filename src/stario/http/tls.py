"""TLS context for hosting Stario directly (not behind a reverse proxy).

ALPN advertises ``h2`` then ``http/1.1`` so one listener can switch once
per connection. HTTP/2 and HTTP/1.1 share the Cython protocol.
"""

from __future__ import annotations

import ssl
from pathlib import Path

from stario.exceptions import StarioError


def load_tls_context(
    *,
    certfile: str | Path,
    keyfile: str | Path | None = None,
    password: str | None = None,
) -> ssl.SSLContext:
    """Server TLS context: TLS 1.2+, ALPN ``h2`` / ``http/1.1``."""
    cert = Path(certfile)
    key = Path(keyfile) if keyfile is not None else None
    if not cert.is_file():
        raise StarioError(
            f"TLS certificate not found: {cert}",
            help_text="Set STARIO_SSL_CERTFILE to an existing PEM certificate.",
        )
    if key is not None and not key.is_file():
        raise StarioError(
            f"TLS private key not found: {key}",
            help_text="Set STARIO_SSL_KEYFILE to the matching PEM private key.",
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(
            str(cert),
            str(key) if key is not None else None,
            password=password,
        )
    except ssl.SSLError as exc:
        raise StarioError(
            f"Failed to load TLS certificate chain: {exc}",
            help_text="Check STARIO_SSL_CERTFILE / STARIO_SSL_KEYFILE PEM files.",
        ) from exc
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    return ctx
