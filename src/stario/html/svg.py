"""
SVG tag factories (PascalCase Python names; element names follow SVG).

Import from the package root or from ``html``—the module is the same object:

    from stario import svg
    # or: from stario.html import svg

Then e.g. ``svg.Circle(...)``. Attribute and child rules match HTML tags (same
``Tag`` implementation in ``stario.html.tag``).
"""

from .tag import Tag as _Tag

# --- root / structure ---------------------------------------------------------

Svg = _Tag("svg")
G = _Tag("g")
Defs = _Tag("defs")
Symbol = _Tag("symbol")
Use = _Tag("use", True)
Marker = _Tag("marker")
Mask = _Tag("mask")
ClipPath = _Tag("clipPath")
Pattern = _Tag("pattern")
LinearGradient = _Tag("linearGradient")
RadialGradient = _Tag("radialGradient")
Stop = _Tag("stop", True)
Filter = _Tag("filter")
ForeignObject = _Tag("foreignObject")
Switch = _Tag("switch")
View = _Tag("view")

# --- shapes ------------------------------------------------------------------

Path = _Tag("path", True)
Rect = _Tag("rect", True)
Circle = _Tag("circle", True)
Ellipse = _Tag("ellipse", True)
Line = _Tag("line", True)
Polyline = _Tag("polyline", True)
Polygon = _Tag("polygon", True)
Image = _Tag("image", True)

# --- text --------------------------------------------------------------------

Text = _Tag("text")
TextPath = _Tag("textPath")
Tspan = _Tag("tspan")

# --- metadata / descriptive --------------------------------------------------

Title = _Tag("title")
Desc = _Tag("desc")
Metadata = _Tag("metadata")

# --- animation ---------------------------------------------------------------

Animate = _Tag("animate")
AnimateMotion = _Tag("animateMotion")
AnimateTransform = _Tag("animateTransform")
Set = _Tag("set")
Mpath = _Tag("mpath", True)

Discard = _Tag("discard", True)

# --- linking -----------------------------------------------------------------

A = _Tag("a")

# --- SVG2 mesh gradients -------------------------------------------------------

MeshGradient = _Tag("meshGradient")
MeshRow = _Tag("meshRow")
MeshPatch = _Tag("meshPatch")

# --- filter primitives (mostly empty leaves) ----------------------------------

FeBlend = _Tag("feBlend", True)
FeColorMatrix = _Tag("feColorMatrix", True)
FeComponentTransfer = _Tag("feComponentTransfer")
FeComposite = _Tag("feComposite", True)
FeConvolveMatrix = _Tag("feConvolveMatrix", True)
FeDiffuseLighting = _Tag("feDiffuseLighting")
FeDisplacementMap = _Tag("feDisplacementMap", True)
FeDistantLight = _Tag("feDistantLight", True)
FeDropShadow = _Tag("feDropShadow", True)
FeFlood = _Tag("feFlood", True)
FeFuncA = _Tag("feFuncA", True)
FeFuncB = _Tag("feFuncB", True)
FeFuncG = _Tag("feFuncG", True)
FeFuncR = _Tag("feFuncR", True)
FeGaussianBlur = _Tag("feGaussianBlur", True)
FeImage = _Tag("feImage", True)
FeMerge = _Tag("feMerge")
FeMergeNode = _Tag("feMergeNode", True)
FeMorphology = _Tag("feMorphology", True)
FeOffset = _Tag("feOffset", True)
FePointLight = _Tag("fePointLight", True)
FeSpecularLighting = _Tag("feSpecularLighting")
FeSpotLight = _Tag("feSpotLight", True)
FeTile = _Tag("feTile", True)
FeTurbulence = _Tag("feTurbulence", True)

# --- misc --------------------------------------------------------------------

Style = _Tag("style")
