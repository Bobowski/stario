"""Shared document shell — feature views supply the body only."""

from app.assets import DATASTAR_JS, STYLE_CSS
from stario.markup import HtmlElement, baked
from stario.markup import html as h


@baked
def page(body: HtmlElement):
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "UTF-8"}),
            h.Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            h.Title("Stario Chat"),
            h.Link({"rel": "stylesheet", "href": STYLE_CSS}),
            h.Script({"type": "module", "src": DATASTAR_JS}),
        ),
        h.Body(body),
    )
