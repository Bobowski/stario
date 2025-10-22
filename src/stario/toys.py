from stario.html import b, body, div, head, html, meta, pre, script, title
from stario.html.core import HtmlElement


def toy_inspector() -> HtmlElement:
    """
    We simply add a div positioned absolutely on the top right of the page
    with the label of key-binding opening the debug panel (CMD+P by default)
    and the content of the debug panel being a pre tag with the attribute
    data-json-signals.

    Should be slightly reduced opacity.

    <pre data-json-signals></pre>
    https://data-star.dev/reference/attributes#data-json-signals
    """

    return div(
        {
            "style": {
                "position": "absolute",
                "top": "1rem",
                "right": "1rem",
                "opacity": "0.95",
                "border": "1px solid #ccc",
                "background": "#fff",
                "padding": "0.75rem",
                "min-width": "220px",
                "z-index": "1000",
            },
        },
        b("Debug Inspector:"),
        pre(
            {
                "data-json-signals": True,
                "style": {
                    "background": "#f4f4f4",
                    "border": "1px solid #eee",
                    "padding": "0.5rem",
                    "margin-bottom": "0.25rem",
                    "font-size": "0.95em",
                    "max-height": "200px",
                    "overflow": "auto",
                },
            }
        ),
    )


load_datastar = script(
    {
        "type": "module",
        "src": "https://cdn.jsdelivr.net/gh/starfederation/datastar@1.0.0-RC.6/bundles/datastar.js",
    },
)


def toy_page(
    *content: HtmlElement,
    page_title: str = "Playground",
) -> HtmlElement:

    return html(
        head(
            meta({"charset": "UTF-8"}),
            title(page_title),
            load_datastar,
        ),
        body(
            *content,
            toy_inspector(),
        ),
    )
