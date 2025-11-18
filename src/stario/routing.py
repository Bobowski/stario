from typing import Any, Callable, Collection, Sequence, TypeVar

from starlette.datastructures import URLPath
from starlette.middleware import Middleware
from starlette.routing import BaseRoute, Router
from starlette.types import ASGIApp, Lifespan, Receive, Scope, Send

from stario.types import HeadersConstraint

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
        self.on_startup: list[Callable[[], Any]] = []
        self.on_shutdown: list[Callable[[], Any]] = []

        self.lifespan_context = lifespan or _DefaultLifespan(self)

        self.middleware_stack = self.app
        if middleware:
            for cls, args, kwargs in reversed(middleware):
                self.middleware_stack = cls(self.middleware_stack, *args, **kwargs)

    async def not_found(self, scope: Scope, receive: Receive, send: Send) -> None:
        await Router.not_found(self, scope, receive, send)  # type: ignore[arg-type]

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        # Starlette Router depends here only on self.routes
        return Router.url_path_for(self, name, **path_params)  # type: ignore[arg-type]

    async def startup(self) -> None:
        # Starlette Router depends here only on self.on_startup
        await Router.startup(self)  # type: ignore[arg-type]

    async def shutdown(self) -> None:
        # Starlette Router depends here only on self.on_shutdown
        await Router.shutdown(self)  # type: ignore[arg-type]

    async def lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Starlette Router depends here only on self.lifespan_context
        await Router.lifespan(self, scope, receive, send)  # type: ignore[arg-type]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.middleware_stack(scope, receive, send)

    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Starlette Router depends here only on self.lifespan(), self.routes and self.default
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

    def query(
        self,
        path: str,
        /,
        *,
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        headers: HeadersConstraint | None = None,
    ) -> Callable[[Callable], Callable]:
        """Decorator for registering query routes.

        Parameters:
            path: The URL path for the route
            methods: HTTP methods for the route. Defaults to ["GET"]
            name: Optional name for the route
            include_in_schema: Whether to include in OpenAPI schema
            middleware: Optional middleware for this specific route
            headers: Optional header constraints for route matching

        Returns:
            A decorator function that registers the endpoint as a query route
        """

        from stario.routes import Query

        def decorator(func: Callable) -> Callable:
            route = Query(
                path,
                func,
                methods=methods or ["GET"],
                name=name,
                include_in_schema=include_in_schema,
                middleware=middleware,
                headers=headers,
            )
            self.add(route)
            return func

        return decorator

    def command(
        self,
        path: str,
        /,
        *,
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        headers: HeadersConstraint | None = None,
    ) -> Callable[[Callable], Callable]:
        """Decorator for registering command routes.

        Parameters:
            path: The URL path for the route
            methods: HTTP methods for the route. Defaults to ["POST"]
            name: Optional name for the route
            include_in_schema: Whether to include in OpenAPI schema
            middleware: Optional middleware for this specific route
            headers: Optional header constraints for route matching

        Returns:
            A decorator function that registers the endpoint as a command route
        """

        from stario.routes import Command

        def decorator(func: Callable) -> Callable:
            route = Command(
                path,
                func,
                methods=methods or ["POST"],
                name=name,
                include_in_schema=include_in_schema,
                middleware=middleware,
                headers=headers,
            )
            self.add(route)
            return func

        return decorator

    def detached_command(
        self,
        path: str,
        /,
        *,
        methods: Collection[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] | None = None,
        headers: HeadersConstraint | None = None,
    ) -> Callable[[Callable], Callable]:
        """Decorator for registering detached command routes.

        Parameters:
            path: The URL path for the route
            methods: HTTP methods for the route. Defaults to ["POST"]
            name: Optional name for the route
            include_in_schema: Whether to include in OpenAPI schema
            middleware: Optional middleware for this specific route
            headers: Optional header constraints for route matching

        Returns:
            A decorator function that registers the endpoint as a detached command route
        """

        from stario.routes import DetachedCommand

        def decorator(func: Callable) -> Callable:
            route = DetachedCommand(
                path,
                func,
                methods=methods or ["POST"],
                name=name,
                include_in_schema=include_in_schema,
                middleware=middleware,
                headers=headers,
            )
            self.add(route)
            return func

        return decorator
