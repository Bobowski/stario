from typing import Any, Callable, Collection, Mapping, Sequence, override

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Match, Route, get_name
from starlette.types import Receive, Scope, Send

from stario.datastar.adapters import detached_command
from stario.datastar.adapters import handler as datastar_handler
from stario.types import (
    AdapterFunction,
    HeadersConstraint,
)


class StarRoute[T](Route):

    def __init__(
        self,
        path: str,
        endpoint: Callable[..., T],
        *,
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        # Stario specific
        headers: HeadersConstraint | None = None,
        adapter: AdapterFunction[T] = datastar_handler(),
    ) -> None:

        # fmt: off
        super().__init__(
            path              = path,
            endpoint          = adapter(endpoint),
            methods           = methods,
            name              = name or get_name(endpoint),
            include_in_schema = include_in_schema,
            middleware        = middleware,
        )
        # fmt: on

        # TODO: Should I consider query=, accepts=, content_type= constraints as typed parameters?

        if headers is None:
            self.headers = {}
        elif isinstance(headers, Mapping):
            self.headers = dict(headers)
        else:
            self.headers = {}
            for h in headers:

                if isinstance(h, tuple):
                    self.headers[h[0]] = h[1]
                else:
                    self.headers[h] = None

        # self.wrapper = wrapper

    def _headers_match(self, headers: Headers) -> bool:
        for k, v in self.headers.items():

            if k not in headers:
                return False

            if v is not None and headers.get(k) != v:
                return False

        return True

    @override
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        # We override this only because we want to support headers constraint

        base_match, base_scope = super().matches(scope)

        # This would fail anyways so we can just return here
        if not self.headers or base_match != Match.FULL:
            return base_match, base_scope

        # I just hope this is light enough so we can create this over and over again
        headers = Headers(scope=scope)
        if not self._headers_match(headers):
            return Match.PARTIAL, base_scope

        # If it's a match anyways, return it :)
        return base_match, base_scope

    @override
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:

        # If there's nothing to handle additionally, just call the super
        if not self.headers:
            await super().handle(scope, receive, send)
            return

        # If there's something to handle, we need to check the headers
        headers = Headers(scope=scope)
        if not self._headers_match(headers):

            headers_str = ", ".join(str(h) for h in self.headers)
            msg = f"Expected the following headers to be present: {headers_str}"

            if "app" in scope:
                raise HTTPException(status_code=400, detail=msg)
            else:
                response = PlainTextResponse(msg, status_code=400)
            await response(scope, receive, send)
            return

        # All good, call the super
        await super().handle(scope, receive, send)

    @override
    def __eq__(self, other: Any) -> bool:
        return super().__eq__(other) and list(self.headers) == list(other.headers)

    @override
    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        methods = sorted(self.methods or [])
        path, name = self.path, self.name
        return f"{class_name}(path={path!r}, name={name!r}, methods={methods!r}, headers={self.headers!r})"


class Query[T](StarRoute[T]):
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., T],
        *,
        methods: Collection[str] | None = ["GET"],
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        # Stario specific
        headers: HeadersConstraint | None = None,
        adapter: AdapterFunction[T] = datastar_handler(),
    ) -> None:

        # fmt: off
        super().__init__(
            path              = path,
            endpoint          = endpoint,
            methods           = methods,
            name              = name,
            include_in_schema = include_in_schema,
            middleware        = middleware,
            headers           = headers,
            adapter           = adapter,
        )
        # fmt: on


class Command[T](StarRoute[T]):
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., T],
        *,
        methods: Collection[str] | None = ["POST"],
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        # Stario specific
        headers: HeadersConstraint | None = None,
        adapter: AdapterFunction[T] = datastar_handler(),
    ) -> None:

        # fmt: off
        super().__init__(
            path              = path,
            endpoint          = endpoint,
            methods           = methods,
            name              = name,
            include_in_schema = include_in_schema,
            middleware        = middleware,
            headers           = headers,
            adapter           = adapter,
        )
        # fmt: on


class DetachedCommand[T](StarRoute[T]):
    def __init__(
        self,
        path: str,
        endpoint: Callable[..., T],
        *,
        methods: Collection[str] | None = ["POST"],
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        # Stario specific
        headers: HeadersConstraint | None = None,
        adapter: AdapterFunction[T] = detached_command(),
    ) -> None:

        # fmt: off
        super().__init__(
            path              = path,
            endpoint          = endpoint,
            methods           = methods,
            name              = name,
            include_in_schema = include_in_schema,
            middleware        = middleware,
            headers           = headers,
            adapter           = adapter,
        )
        # fmt: on
