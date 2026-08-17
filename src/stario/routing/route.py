"""HTTP request identity: one method on one `UrlPath`.

`UrlPath` is a location (compose, `href()`, middleware prefixes). `Route` is the
leaf you register and call from Datastar: method plus that location. It does not
compose with `/` and does not hold a handler.
"""

from collections.abc import Mapping

from stario.exceptions import StarioError
from stario.routing.urlpath import UrlPath


class Route:
    """One HTTP method on one path template.

    ```python
    HOME = Route.get("/")
    SEND = Route.post(UrlPath("/rooms") / "{room_id}" / "send")

    app.add(SEND, send)
    at.fetch(SEND, {"room_id": room.id})
    ```
    """

    __slots__ = ("method", "path")

    def __init__(self, method: str, path: str | UrlPath) -> None:
        token = method.strip().upper()
        if not token or any(ch.isspace() for ch in token):
            raise StarioError(
                "Route method must be a single HTTP token",
                context={"method": method},
                help_text="Use Route.get(path), Route.post(path), or Route('PROPFIND', path).",
            )
        self.method = token
        self.path = path if isinstance(path, UrlPath) else UrlPath(path)

    @classmethod
    def get(cls, path: str | UrlPath) -> Route:
        return cls("GET", path)

    @classmethod
    def query(cls, path: str | UrlPath) -> Route:
        return cls("QUERY", path)

    @classmethod
    def post(cls, path: str | UrlPath) -> Route:
        return cls("POST", path)

    @classmethod
    def put(cls, path: str | UrlPath) -> Route:
        return cls("PUT", path)

    @classmethod
    def patch(cls, path: str | UrlPath) -> Route:
        return cls("PATCH", path)

    @classmethod
    def delete(cls, path: str | UrlPath) -> Route:
        return cls("DELETE", path)

    @classmethod
    def head(cls, path: str | UrlPath) -> Route:
        return cls("HEAD", path)

    @classmethod
    def options(cls, path: str | UrlPath) -> Route:
        return cls("OPTIONS", path)

    def href(
        self,
        params: Mapping[str, object] | None = None,
        /,
        *,
        query: Mapping[str, object] | None = None,
        fragment: str | None = None,
        **params_kwargs: object,
    ) -> str:
        """Build the browser URL for this path. Same contract as `UrlPath.href()`."""
        return self.path.href(params, query=query, fragment=fragment, **params_kwargs)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Route)
            and self.method == other.method
            and self.path.text == other.path.text
        )

    def __hash__(self) -> int:
        return hash((self.method, self.path.text))

    def __repr__(self) -> str:
        return f"Route({self.method!r}, {self.path!r})"
