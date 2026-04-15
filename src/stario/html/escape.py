"""HTML escaping for text nodes and attribute names/values."""

from functools import lru_cache

from .constants import COMMON_SAFE_ATTRIBUTE_NAMES


def escape_text(s: str) -> str:
    """Escape ``&``, ``<``, ``>`` for text nodes (quotes stay literal)."""
    # Fast path: most copy has no entities; avoids allocating a new string.
    if "&" not in s and "<" not in s and ">" not in s:
        return s

    # ``&`` first — otherwise ``&lt;`` would become ``&amp;lt;``.
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attribute_value(s: str) -> str:
    """Escape for double-quoted attribute values (includes quotes)."""
    # Straight replaces beat html.escape on hot paths we measured.
    if (
        "&" not in s
        and "<" not in s
        and ">" not in s
        and '"' not in s
        and "'" not in s
    ):
        return s

    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def escape_attribute_key(k: str) -> str:
    """Escape unusual attribute *names* (dynamic keys, not common static ones)."""
    return (
        escape_attribute_value(k)
        .replace("=", "&#x3D;")
        .replace("\\", "&#x5C;")
        .replace("`", "&#x60;")
        .replace(" ", "&nbsp;")
    )


@lru_cache(maxsize=256)
def _normalize_attribute_key(key: str) -> str:
    # Known-safe names skip escaping; everything else goes through escape_attribute_key.
    if key in COMMON_SAFE_ATTRIBUTE_NAMES:
        return key
    return escape_attribute_key(key)
