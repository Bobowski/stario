"""Shared static assets for the chat-room example."""

from pathlib import Path

from stario import Assets

# href() is cheap. attach(app) in bootstrap registers routes and loads files.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS = Assets(PROJECT_ROOT / "static", "/static")
STYLE_CSS = ASSETS.href("css/style.css")
DATASTAR_JS = ASSETS.href("js/datastar.js")
