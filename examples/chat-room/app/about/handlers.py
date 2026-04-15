"""Small handlers for the about area (no extra deps — shows a second mount)."""

import stario.responses as responses
from stario import Context, Writer


async def index(c: Context, w: Writer) -> None:
    responses.text(
        w,
        "This route lives in app.about and is mounted at /about in main.bootstrap.",
        200,
    )
