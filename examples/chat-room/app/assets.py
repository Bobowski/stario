"""Shared static assets for the chat-room example."""

from pathlib import Path

from stario import AssetManifest

# Cheap at import time: scan + fingerprint only. Serving (compression, caching)
# is paid in bootstrap when StaticAssets wraps the manifest.
ASSETS = AssetManifest(Path(__file__).resolve().parent / "static")
STYLE_CSS = ASSETS.href("css/style.css")
DATASTAR_JS = ASSETS.href("js/datastar.js")
