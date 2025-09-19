from typing import Any, Callable, Collection, Sequence

from starlette.middleware import Middleware
from starlette.routing import get_name

from stario.routing import HeadersConstraint, StarRoute
from stario.wrappers import QuickRouteWrapper


class Query(StarRoute):
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        methods: Collection[str] | None = ["GET"],
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        # Stario specific
        headers: HeadersConstraint | None = None,
    ) -> None:
        # fmt: off
        super().__init__(
            path              = path,
            endpoint          = QuickRouteWrapper(endpoint).__call__,
            methods           = methods,
            name              = name or get_name(endpoint),
            include_in_schema = include_in_schema,
            middleware        = middleware,
            headers           = headers,
        )
        # fmt: on


class Command(StarRoute):
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        methods: Collection[str] | None = ["POST"],
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        # Stario specific
        headers: HeadersConstraint | None = None,
    ) -> None:

        # fmt: off
        super().__init__(
            path              = path,
            endpoint          = QuickRouteWrapper(endpoint).__call__,
            methods           = methods,
            name              = name or get_name(endpoint),
            include_in_schema = include_in_schema,
            middleware        = middleware,
            headers           = headers,
        )
        # fmt: on


class CommandDetached(StarRoute):
    pass
