"""Query pair list. The implementation is ``stario_cython.exchange.ParsedQuery``.

First read indexes C name/value spans on the original bytes when names need no
in-place unquote; ``get`` / ``getlist`` search those and decode only the values
you ask for.
"""

from stario_cython.exchange import ParsedQuery

__all__ = ["ParsedQuery"]
