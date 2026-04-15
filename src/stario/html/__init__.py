"""Public HTML API for Stario."""

from . import svg as svg
from . import tags as tags
from .baked import baked as baked
from .render import render as render
from .tag import Tag as Tag

# Keep the tag catalog in one file so this module stays readable.
from .tags import *  # noqa: F403
from .types import Comment as Comment
from .types import HtmlElement as HtmlElement
from .types import SafeString as SafeString
