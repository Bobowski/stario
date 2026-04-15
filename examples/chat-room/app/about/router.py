"""Merged from ``main`` with ``app.mount("/", build_router())`` — paths are app-root absolute."""

from stario import Router

from .handlers import index


def build_router() -> Router:
    r = Router()
    r.get("/about", index, name="about_index")
    return r
