"""
SVG tag factories (PascalCase Python names; element names follow SVG).

Import from the markup namespace:

    from stario.markup import svg

Then e.g. `svg.Circle(...)`. Attribute and child rules match HTML tags (same
`Tag` implementation). SVG uses camelCase attribute names (`viewBox`,
`stdDeviation`) and literal colon keys (`"xlink:href"`). For standalone SVG
documents, set `xmlns` on the root `svg.Svg` element.
"""

from .tag import Tag as _Tag

# --- root / structure ---------------------------------------------------------

Svg = _Tag("svg")
G = _Tag("g")
Defs = _Tag("defs")
Symbol = _Tag("symbol")
Use = _Tag("use", empty="self_closing_when_empty")
Marker = _Tag("marker")
Mask = _Tag("mask")
ClipPath = _Tag("clipPath")
Pattern = _Tag("pattern")
LinearGradient = _Tag("linearGradient")
RadialGradient = _Tag("radialGradient")
Stop = _Tag("stop", empty="self_closing_when_empty")
Filter = _Tag("filter")
ForeignObject = _Tag("foreignObject")
Switch = _Tag("switch")
View = _Tag("view")

# --- shapes ------------------------------------------------------------------

Path = _Tag("path", empty="self_closing_when_empty")
Rect = _Tag("rect", empty="self_closing_when_empty")
Circle = _Tag("circle", empty="self_closing_when_empty")
Ellipse = _Tag("ellipse", empty="self_closing_when_empty")
Line = _Tag("line", empty="self_closing_when_empty")
Polyline = _Tag("polyline", empty="self_closing_when_empty")
Polygon = _Tag("polygon", empty="self_closing_when_empty")
Image = _Tag("image", empty="self_closing_when_empty")

# --- text --------------------------------------------------------------------

Text = _Tag("text")
TextPath = _Tag("textPath")
Tspan = _Tag("tspan")

# --- metadata / descriptive --------------------------------------------------

Title = _Tag("title")
Desc = _Tag("desc")
Metadata = _Tag("metadata")

# --- animation ---------------------------------------------------------------

Animate = _Tag("animate", empty="self_closing_when_empty")
AnimateMotion = _Tag("animateMotion", empty="self_closing_when_empty")
AnimateTransform = _Tag("animateTransform", empty="self_closing_when_empty")
Set = _Tag("set", empty="self_closing_when_empty")
Mpath = _Tag("mpath", empty="self_closing_when_empty")

Discard = _Tag("discard", empty="self_closing_when_empty")

# --- linking -----------------------------------------------------------------

A = _Tag("a")

# --- SVG2 mesh / hatch (lowercase wire names per SVG2 resolution) ------------
# Deferred from SVG 2 with no browser support; kept for authoring (e.g. Inkscape).
Mesh = _Tag("mesh", empty="self_closing_when_empty")
MeshGradient = _Tag("meshgradient")
MeshRow = _Tag("meshrow")
MeshPatch = _Tag("meshpatch")
Hatch = _Tag("hatch")
Hatchpath = _Tag("hatchpath", empty="self_closing_when_empty")

# --- filter primitives (mostly empty leaves) ----------------------------------

FeBlend = _Tag("feBlend", empty="self_closing_when_empty")
FeColorMatrix = _Tag("feColorMatrix", empty="self_closing_when_empty")
FeComponentTransfer = _Tag("feComponentTransfer")
FeComposite = _Tag("feComposite", empty="self_closing_when_empty")
FeConvolveMatrix = _Tag("feConvolveMatrix", empty="self_closing_when_empty")
FeDiffuseLighting = _Tag("feDiffuseLighting")
FeDisplacementMap = _Tag("feDisplacementMap", empty="self_closing_when_empty")
FeDistantLight = _Tag("feDistantLight", empty="self_closing_when_empty")
FeDropShadow = _Tag("feDropShadow", empty="self_closing_when_empty")
FeFlood = _Tag("feFlood", empty="self_closing_when_empty")
FeFuncA = _Tag("feFuncA", empty="self_closing_when_empty")
FeFuncB = _Tag("feFuncB", empty="self_closing_when_empty")
FeFuncG = _Tag("feFuncG", empty="self_closing_when_empty")
FeFuncR = _Tag("feFuncR", empty="self_closing_when_empty")
FeGaussianBlur = _Tag("feGaussianBlur", empty="self_closing_when_empty")
FeImage = _Tag("feImage", empty="self_closing_when_empty")
FeMerge = _Tag("feMerge")
FeMergeNode = _Tag("feMergeNode", empty="self_closing_when_empty")
FeMorphology = _Tag("feMorphology", empty="self_closing_when_empty")
FeOffset = _Tag("feOffset", empty="self_closing_when_empty")
FePointLight = _Tag("fePointLight", empty="self_closing_when_empty")
FeSpecularLighting = _Tag("feSpecularLighting")
FeSpotLight = _Tag("feSpotLight", empty="self_closing_when_empty")
FeTile = _Tag("feTile", empty="self_closing_when_empty")
FeTurbulence = _Tag("feTurbulence", empty="self_closing_when_empty")

# --- misc --------------------------------------------------------------------

Script = _Tag("script")
Style = _Tag("style")
