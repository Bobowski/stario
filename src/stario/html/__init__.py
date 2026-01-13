"""
HTML - Declarative HTML generation.

Build HTML with Python functions, no template files needed.
Inspired by simple-html (https://github.com/keithasaurus/simple_html).

Why not Jinja2/Mako/etc?
- Type safety: IDE autocomplete, refactoring works
- Performance: No template parsing at runtime
- Simplicity: Just Python, no new syntax to learn
- Composability: Functions all the way down

Usage:
    from stario.html import Div, P, render
    from stario.datastar import at, data

    html = render(
        Div({"class": "container"}, at.get("/api/data"),
            P("Hello, World!")
        )
    )

Key concepts:
- Tag: A callable that creates HTML elements (Div, P, Span, etc.)
- SafeString: Content that won't be escaped (for raw HTML)
- render(): Convert elements to a string

Datastar helpers (import from stario.datastar):
- at: Action builders (at.get(), at.post())
- data: Attribute builders (data.signals(), data.on())
"""

from .core import HtmlElement as HtmlElement
from .core import SafeString as SafeString
from .core import Tag as Tag
from .core import TagAttributes as TagAttributes
from .core import render as render

# Doctype + html tag in one - because you almost always want both
DOCTYPE_HTML5 = SafeString("<!doctype html>")
OnlyHtml = Tag("html")

# Html tag with doctype pre-baked
# 99% of the time you want <!doctype html><html>...</html>
Html = Tag("html")
Html.tag_start = "<!doctype html><html"
Html.tag_start_no_attrs = "<!doctype html><html>"
Html.closing_tag = "</html>"
Html.no_children_close = "></html>"
Html.rendered = "<!doctype html><html></html>"

# =============================================================================
# Standard HTML tags
# =============================================================================
# Most tags are regular: <tag>content</tag>
# Self-closing tags (void elements) use: <tag/>

A = Tag("a")
Abbr = Tag("abbr")
Address = Tag("address")
Area = Tag("area", True)  # Self-closing
Article = Tag("article")
Aside = Tag("aside")
Audio = Tag("audio")
B = Tag("b")
Base = Tag("base", True)
Bdi = Tag("bdi")
Bdo = Tag("bdo")
Blockquote = Tag("blockquote")
Body = Tag("body")
Br = Tag("br", True)
Button = Tag("button")
Canvas = Tag("canvas")
Center = Tag("center")
Caption = Tag("caption")
Cite = Tag("cite")
Code = Tag("code")
Col = Tag("col")
Colgroup = Tag("colgroup")
Data = Tag("data")
Datalist = Tag("datalist")
Dd = Tag("dd")
Details = Tag("details")
Del = Tag("del")
Dialog = Tag("dialog")
Dfn = Tag("dfn")
Div = Tag("div")
Dl = Tag("dl")
Dt = Tag("dt")
Em = Tag("em")
Embed = Tag("embed", True)
Fieldset = Tag("fieldset")
Figure = Tag("figure")
Figcaption = Tag("figcaption")
Footer = Tag("footer")
Font = Tag("font")
Form = Tag("form")
Head = Tag("head")
Header = Tag("header")
Hgroup = Tag("hgroup")
H1 = Tag("h1")
H2 = Tag("h2")
H3 = Tag("h3")
H4 = Tag("h4")
H5 = Tag("h5")
H6 = Tag("h6")
Hr = Tag("hr", True)
I = Tag("i")  # noqa: E741
Iframe = Tag("iframe")
Img = Tag("img", True)
Input = Tag("input", True)
Ins = Tag("ins")
Kbd = Tag("kbd")
Label = Tag("label")
Legend = Tag("legend")
Li = Tag("li")
Link = Tag("link", True)
Main = Tag("main")
Mark = Tag("mark")
Marquee = Tag("marquee")
Math = Tag("math")
Menu = Tag("menu")
Menuitem = Tag("menuitem")
Meta = Tag("meta", True)
Meter = Tag("meter")
Nav = Tag("nav")
Object = Tag("object")
Noscript = Tag("noscript")
Ol = Tag("ol")
Optgroup = Tag("optgroup")
Option = Tag("option")
Output = Tag("output")
P = Tag("p")
Param = Tag("param", True)
Picture = Tag("picture")
Pre = Tag("pre")
Progress = Tag("progress")
Q = Tag("q")
Rp = Tag("rp")
Rt = Tag("rt")
Ruby = Tag("ruby")
S = Tag("s")
Samp = Tag("samp")
Script = Tag("script")
Search = Tag("search")
Section = Tag("section")
Select = Tag("select")
Small = Tag("small")
Source = Tag("source", True)
Span = Tag("span")
Strike = Tag("strike")
Strong = Tag("strong")
Style = Tag("style")
Sub = Tag("sub")
Summary = Tag("summary")
Sup = Tag("sup")

# SVG elements (preserve original casing for SVG-specific elements)
Svg = Tag("svg")
Rect = Tag("rect", True)
Circle = Tag("circle", True)
Ellipse = Tag("ellipse", True)
Line = Tag("line", True)
Polyline = Tag("polyline", True)
Polygon = Tag("polygon", True)
Path = Tag("path", True)
G = Tag("g")
Defs = Tag("defs")
LinearGradient = Tag("linearGradient")
RadialGradient = Tag("radialGradient")
Stop = Tag("stop", True)
Text = Tag("text")
Tspan = Tag("tspan")
ClipPath = Tag("clipPath")
Mask = Tag("mask")
Filter = Tag("filter")
Pattern = Tag("pattern")
Symbol = Tag("symbol")
Use = Tag("use", True)
Image = Tag("image", True)
Marker = Tag("marker")

# Table elements
Table = Tag("table")
Tbody = Tag("tbody")
Tfoot = Tag("tfoot")
Template = Tag("template")
Textarea = Tag("textarea")
Td = Tag("td")
Th = Tag("th")
Thead = Tag("thead")
Time = Tag("time")
Title = Tag("title")
Tr = Tag("tr")
Track = Tag("track", True)
U = Tag("u")
Ul = Tag("ul")
Var = Tag("var")
Video = Tag("video")
Wbr = Tag("wbr")

# =============================================================================
# __all__ for `from stario.html import *`
# =============================================================================
__all__ = [
    # Core exports
    "HtmlElement",
    "SafeString",
    "Tag",
    "TagAttributes",
    "render",
    # Special
    "DOCTYPE_HTML5",
    "OnlyHtml",
    "Html",
    # Standard HTML tags
    "A",
    "Abbr",
    "Address",
    "Area",
    "Article",
    "Aside",
    "Audio",
    "B",
    "Base",
    "Bdi",
    "Bdo",
    "Blockquote",
    "Body",
    "Br",
    "Button",
    "Canvas",
    "Center",
    "Caption",
    "Cite",
    "Code",
    "Col",
    "Colgroup",
    "Data",
    "Datalist",
    "Dd",
    "Details",
    "Del",
    "Dfn",
    "Dialog",
    "Div",
    "Dl",
    "Dt",
    "Em",
    "Embed",
    "Fieldset",
    "Figure",
    "Figcaption",
    "Footer",
    "Font",
    "Form",
    "Head",
    "Header",
    "Hgroup",
    "H1",
    "H2",
    "H3",
    "H4",
    "H5",
    "H6",
    "Hr",
    "I",
    "Iframe",
    "Img",
    "Input",
    "Ins",
    "Kbd",
    "Label",
    "Legend",
    "Li",
    "Link",
    "Main",
    "Mark",
    "Marquee",
    "Math",
    "Menu",
    "Menuitem",
    "Meta",
    "Meter",
    "Nav",
    "Object",
    "Noscript",
    "Ol",
    "Optgroup",
    "Option",
    "Output",
    "P",
    "Param",
    "Picture",
    "Pre",
    "Progress",
    "Q",
    "Rp",
    "Rt",
    "Ruby",
    "S",
    "Samp",
    "Script",
    "Search",
    "Section",
    "Select",
    "Small",
    "Source",
    "Span",
    "Strike",
    "Strong",
    "Style",
    "Sub",
    "Summary",
    "Sup",
    # SVG elements
    "Svg",
    "Rect",
    "Circle",
    "Ellipse",
    "Line",
    "Polyline",
    "Polygon",
    "Path",
    "G",
    "Defs",
    "LinearGradient",
    "RadialGradient",
    "Stop",
    "Text",
    "Tspan",
    "ClipPath",
    "Mask",
    "Filter",
    "Pattern",
    "Symbol",
    "Use",
    "Image",
    "Marker",
    # Table elements
    "Table",
    "Tbody",
    "Tfoot",
    "Template",
    "Textarea",
    "Td",
    "Th",
    "Thead",
    "Time",
    "Title",
    "Tr",
    "Track",
    "U",
    "Ul",
    "Var",
    "Video",
    "Wbr",
]
