"""HTML escaping for text nodes and attribute names/values."""

from functools import lru_cache


def escape_text(s: str) -> str:
    """Escape ``&``, ``<``, ``>`` for text nodes (quotes stay literal).

    Not LRU-cached: text nodes are usually high-cardinality user copy, so cache
    misses and eviction would dominate versus short-circuit + replace work.
    """
    if "&" not in s and "<" not in s and ">" not in s:
        return s

    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@lru_cache(maxsize=512)
def escape_attribute_value(s: str) -> str:
    """Escape for double-quoted attribute values (includes quotes).

    Memoized for repeated values (classes, ``type``, ``role``, Datastar paths, etc.).
    """
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


@lru_cache(maxsize=256)
def escape_attribute_key(k: str) -> str:
    """Escape attribute *names* for the wire format (dynamic keys, ``data-*`` suffixes, …).

    Cached so tag rendering and nested ``data``/``aria`` dicts share one memoization
    path for each distinct key string.
    """
    return (
        escape_attribute_value(k)
        .replace("=", "&#x3D;")
        .replace("\\", "&#x5C;")
        .replace("`", "&#x60;")
        .replace(" ", "&nbsp;")
    )
