"""
SafeString - Content that bypasses HTML escaping.

WARNING: SafeString bypasses XSS protection. NEVER use with user-controlled content!

When you render HTML, all strings are escaped to prevent XSS attacks.
SafeString marks content as "already safe" - use with extreme care!

Usage:
    from stario.html import SafeString, Div

    # Regular strings are escaped (SAFE - default behavior)
    Div("<script>alert('xss')</script>")
    # -> <div>&lt;script&gt;alert('xss')&lt;/script&gt;</div>

    # SafeString bypasses escaping (DANGEROUS if content is untrusted)
    Div(SafeString("<b>bold</b>"))
    # -> <div><b>bold</b></div>

SAFE uses:
- Hardcoded HTML literals in your code
- Pre-rendered markdown from trusted source (your own docs)
- Trusted SVG icons from your own asset files
- Output from your own render() function

DANGEROUS uses (XSS vulnerability):
- User input (form fields, comments, usernames)
- Content from external APIs
- Database content that came from users
- URL parameters or query strings
- Anything not 100% controlled by you

If you need to include HTML from untrusted sources, use a sanitization
library like bleach or nh3 first, then wrap the sanitized output.
"""

from typing import Any


class SafeString:
    """
    A string that will not be escaped when rendered to HTML.

    The internal safe_str attribute holds the raw content.
    Hashable so it can be used as dict keys (for caching).
    """

    __slots__ = ("safe_str",)

    def __init__(self, safe_str: str) -> None:
        self.safe_str = safe_str

    def __hash__(self) -> int:
        return hash(("SafeString", self.safe_str))

    def __eq__(self, other: Any) -> bool:
        return type(other) is SafeString and other.safe_str == self.safe_str

    def __repr__(self) -> str:
        return f"SafeString('{self.safe_str}')"
