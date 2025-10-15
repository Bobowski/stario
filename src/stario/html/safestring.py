from typing import Any


class SafeString:
    """
    A string that will not be escaped when rendered to HTML.
    """

    def __init__(self, safe_str: str) -> None:
        self.safe_str = safe_str

    def __hash__(self) -> int:
        return hash(("SafeString", self.safe_str))

    def __eq__(self, other: Any) -> bool:
        return type(other) is SafeString and other.safe_str == self.safe_str

    def __repr__(self) -> str:
        return f"SafeString('{self.safe_str}')"
