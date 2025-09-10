from typing import Any, Callable, Collection, Mapping, Sequence, TypeVar, override

from starlette.datastructures import Headers, URLPath
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Match, Route, Router
from starlette.types import ASGIApp, Lifespan, Receive, Scope, Send

type HeadersConstraint = Mapping[str, str | None] | Sequence[
    str | tuple[str, str | None]
]


class StarRoute(Route):

    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        # Stario specific
        headers: HeadersConstraint | None = None,
        # wrapper: type[HandlerWrapper] = QuickRouteWrapper,
    ) -> None:

        # fmt: off
        super().__init__(
            path              = path,
            endpoint          = endpoint,
            methods           = methods,
            name              = name,
            include_in_schema = include_in_schema,
            middleware        = middleware,
        )
        # fmt: on

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


_T = TypeVar("_T")


class _DefaultLifespan:
    def __init__(self, router: "StarRouter"):
        self._router = router

    async def __aenter__(self) -> None:
        await self._router.startup()

    async def __aexit__(self, *exc_info: object) -> None:
        await self._router.shutdown()

    def __call__(self: _T, app: object) -> _T:
        return self


class StarRouter:
    def __init__(
        self,
        *routes: BaseRoute,
        redirect_slashes: bool = True,
        default: ASGIApp | None = None,
        lifespan: Lifespan[Any] | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:

        # This is basically borrowed from Starlette's Router
        self.routes = list(routes)
        self.redirect_slashes = redirect_slashes
        self.default = self.not_found if default is None else default
        self.on_startup = []
        self.on_shutdown = []

        self.lifespan_context = lifespan or _DefaultLifespan(self)

        self.middleware_stack = self.app
        if middleware:
            for cls, args, kwargs in reversed(middleware):
                self.middleware_stack = cls(self.middleware_stack, *args, **kwargs)

    async def not_found(self, scope: Scope, receive: Receive, send: Send) -> None:
        await Router.not_found(self, scope, receive, send)  # type: ignore[arg-type]

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        return Router.url_path_for(self, name, **path_params)  # type: ignore[arg-type]

    async def startup(self) -> None:
        await Router.startup(self)  # type: ignore[arg-type]

    async def shutdown(self) -> None:
        await Router.shutdown(self)  # type: ignore[arg-type]

    async def lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        await Router.lifespan(self, scope, receive, send)  # type: ignore[arg-type]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.middleware_stack(scope, receive, send)

    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        await Router.app(self, scope, receive, send)  # type: ignore[arg-type]

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, StarRouter) and self.routes == other.routes

    def mount(self, path: str, app: ASGIApp, name: str | None = None) -> None:
        Router.mount(self, path, app=app, name=name)  # type: ignore[arg-type]

    def host(self, host: str, app: ASGIApp, name: str | None = None) -> None:
        Router.host(self, host, app=app, name=name)  # type: ignore[arg-type]

    def add(self, route: BaseRoute) -> None:
        self.routes.append(route)

    def add_event_handler(self, event_type: str, func: Callable[[], Any]) -> None:
        Router.add_event_handler(self, event_type, func)  # type: ignore[arg-type]
