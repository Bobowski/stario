"""Debug UI helpers for local development."""

from typing import Literal

from stario.datastar import data
from stario.markup import HtmlElement, styles
from stario.markup import html as h


def debug_inspector(
    position: Literal[
        "top-left", "top-right", "bottom-left", "bottom-right"
    ] = "bottom-right",
) -> HtmlElement:
    """Draggable overlay that shows live Datastar signal JSON.

    Omit in production; gate with `if config.debug:` in the view.

    ```python
    from stario.debug import debug_inspector

    page(debug_inspector(), ...)
    ```
    """
    vertical, horizontal = position.split("-", 1)

    return h.Div(
        {"id": "__stario_debug_inspector"},
        styles(
            {
                "position": "fixed",
                "opacity": "0.95",
                "border": "1px solid #ccc",
                "background": "#fff",
                "padding": "0.75rem",
                "width": "320px",
                "z-index": "9999",
                "cursor": "grab",
                "user-select": "none",
                "border-radius": "4px",
                "box-shadow": "0 2px 8px rgba(0,0,0,0.15)",
                vertical: "1rem",
                horizontal: "1rem",
            }
        ),
        # Drag the panel from anywhere except the <pre>, so signal JSON stays selectable.
        data.on(
            "mousedown",
            """
            if (evt.target.tagName !== 'PRE') {
                el.dataset.drag = '1';
                el.dataset.ox = evt.clientX - el.getBoundingClientRect().left;
                el.dataset.oy = evt.clientY - el.getBoundingClientRect().top;
                el.style.cursor = 'grabbing';
            }
            """,
        ),
        data.on(
            "mousemove",
            """
            if (el.dataset.drag) {
                el.style.left = (evt.clientX - el.dataset.ox) + 'px';
                el.style.top = (evt.clientY - el.dataset.oy) + 'px';
                el.style.right = 'auto';
                el.style.bottom = 'auto';
            }
            """,
            target="document",
        ),
        data.on(
            "mouseup",
            "delete el.dataset.drag; el.style.cursor = 'grab'",
            target="document",
        ),
        h.B(styles({"cursor": "grab"}), "Signals Inspector"),
        h.Pre(
            data.json_signals(),
            styles(
                {
                    "background": "#f4f4f4",
                    "border": "1px solid #eee",
                    "padding": "0.5rem",
                    "margin-top": "0.5rem",
                    "margin-bottom": "0",
                    "font-size": "0.85em",
                    "max-height": "200px",
                    "overflow-x": "hidden",
                    "overflow-y": "auto",
                    "text-overflow": "ellipsis",
                    "display": "block",
                    "cursor": "text",
                    "user-select": "text",
                }
            ),
        ),
    )
