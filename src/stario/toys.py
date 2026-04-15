from typing import Literal

from stario import datastar as ds
from stario.html import B, Div, HtmlElement, Pre


def toy_inspector(
    position: Literal[
        "top-left", "top-right", "bottom-left", "bottom-right"
    ] = "top-right",
) -> HtmlElement:
    """Debug overlay: renders signal JSON and draggable via Datastar events (state in ``dataset``)."""
    style = {
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
    }

    if "top" in position:
        style["top"] = "1rem"
    elif "bottom" in position:
        style["bottom"] = "1rem"

    if "left" in position:
        style["left"] = "1rem"
    elif "right" in position:
        style["right"] = "1rem"

    return Div(
        # Mousedown: start drag, store offsets in dataset
        ds.on(
            "mousedown",
            """
            if (evt.target.tagName !== 'PRE') {
                el.dataset.drag = 1;
                el.dataset.ox = evt.clientX - el.getBoundingClientRect().left;
                el.dataset.oy = evt.clientY - el.getBoundingClientRect().top;
                el.style.cursor = 'grabbing';
            }
            """,
        ),
        # Mousemove on window: update position while dragging
        ds.on(
            "mousemove",
            """
            if (el.dataset.drag) {
                el.style.left = (evt.clientX - el.dataset.ox) + 'px';
                el.style.top = (evt.clientY - el.dataset.oy) + 'px';
                el.style.right = 'auto';
                el.style.bottom = 'auto';
            }
            """,
        ),
        # Mouseup on window: stop drag
        ds.on(
            "mouseup",
            "delete el.dataset.drag; el.style.cursor = 'grab'",
        ),
        {
            "id": "__stario_inspector",
            "style": style,
        },
        B(
            {"style": {"cursor": "grab"}},
            "Signals Inspector",
        ),
        Pre(
            ds.json_signals(),
            {
                "style": {
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
                },
            },
        ),
    )


inspector = toy_inspector
