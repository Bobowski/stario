from stario.datastar import LOAD_DATASTAR


def ToyInspector() -> str:
    """
    We simply add a div positioned absolutely on the top right of the page
    with the label of key-binding opening the debug panel (CMD+P by default)
    and the content of the debug panel being a pre tag with the attribute
    data-json-signals.

    Should be slightly reduced opacity.

    <pre data-json-signals></pre>
    https://data-star.dev/reference/attributes#data-json-signals
    """

    # Define simple CSS for the debug panel
    debug_panel_css = """
<style>
.stario-debug-panel {
    position: absolute;
    top: 1rem;
    right: 1rem;
    opacity: 0.95;
    border: 1px solid #ccc;
    background: #fff;
    padding: 0.75rem;
    min-width: 220px;
    z-index: 1000;
}
.stario-debug-pre {
    background: #f4f4f4;
    border: 1px solid #eee;
    padding: 0.5rem;
    margin-bottom: 0.25rem;
    font-size: 0.95em;
    max-height: 200px;
    overflow: auto;
}
</style>
"""

    debug_panel_html = """
<div class="stario-debug-panel">
    <b>Debug Inspector:</b>
    <pre class="stario-debug-pre" data-json-signals></pre>
</div>
"""

    return f"""{debug_panel_css}{debug_panel_html}"""


def ToyPage(contents: str, title: str = "Playground") -> str:
    return f"""
<html>
    <head>
        <title>{title}</title>
        {LOAD_DATASTAR}
    </head>
    <body>
        {contents}
        {ToyInspector()}
    </body>
</html>
"""
