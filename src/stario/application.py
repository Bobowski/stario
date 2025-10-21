import types
from contextlib import contextmanager
from typing import (
    Annotated,
    Any,
    Callable,
    Collection,
    Generator,
    Mapping,
    ParamSpec,
    Protocol,
    Sequence,
    TypeAliasType,
    TypeVar,
    get_args,
    get_origin,
)

from starlette.datastructures import State, URLPath
from starlette.middleware import Middleware
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, ExceptionHandler, Lifespan, Receive, Scope, Send

from stario.exceptions import MiddlewareError
from stario.logging.queue import LogQueue
from stario.middlewares.brotli import BrotliMiddleware
from stario.middlewares.guardian import GuardianMiddleware
from stario.routing import StarRouter
from stario.types import HeadersConstraint

AppType = TypeVar("AppType", bound="Stario")

P = ParamSpec("P")


class _MiddlewareFactory(Protocol[P]):
    def __call__(
        self, app: ASGIApp, /, *args: P.args, **kwargs: P.kwargs
    ) -> ASGIApp: ...


class Stario:
    """
    Creates a Stario application.
    It's 'almost' Starlette app, but we push on some of the details.
    """

    def __init__(
        self: AppType,
        *routes: BaseRoute,
        middleware: Sequence[Middleware] | None = None,
        compression_middleware: Middleware | None = BrotliMiddleware.as_middleware(),
        exception_handlers: Mapping[Any, ExceptionHandler] | None = None,
        lifespan: Lifespan[AppType] | None = None,
        debug: bool = False,
        router_class: type[StarRouter] = StarRouter,
        log_sinks: Sequence[Any] | None = None,
    ) -> None:
        """Initializes the application.

        Parameters:
            routes: A list of routes to serve incoming HTTP and WebSocket requests.
            middleware: A list of middleware to run for every request. A starlette
                application will always automatically include two middleware classes.
                `ServerErrorMiddleware` is added as the very outermost middleware, to handle
                any uncaught errors occurring anywhere in the entire stack.
                `ExceptionMiddleware` is added as the very innermost middleware, to deal
                with handled exception cases occurring in the routing or endpoints.
            compression_middleware: A middleware class to compress the responses.
                By default we opt for brotli compression with gzip fallback.
                Parameters are what we think are reasonable for most use cases.
                If you need tweaking those try `BrotliMiddleware.as_middleware()`.
            exception_handlers: A mapping of either integer status codes,
                or exception class types onto callables which handle the exceptions.
                Exception handler callables should be of the form
                `handler(request, exc) -> response` and may be either standard functions, or
                async functions.
            lifespan: A lifespan context function, which can be used to perform
                startup and shutdown tasks. This is a newer style that replaces the
                `on_startup` and `on_shutdown` handlers. Use one or the other, not both.
            debug: Boolean indicating if debug tracebacks should be returned on errors.
            router_class: A class to use for the router. By default we use `StarRouter`.
                You can use this to customize the behaviour of the app, just consider
                what are the implications :)
            log_sinks: Optional list of log sinks to use. If None, defaults are chosen
                based on debug mode (RichConsoleSink for debug, JSONSink for production).
        """

        self.debug = debug
        self.state = State()
        self.router = router_class(*routes, lifespan=lifespan)
        self.exception_handlers = (
            {} if exception_handlers is None else dict(exception_handlers)
        )
        if compression_middleware is not None:
            middleware = [] if middleware is None else list(middleware)
            middleware.insert(0, compression_middleware)
        self.user_middleware = [] if middleware is None else list(middleware)
        self.middleware_stack: ASGIApp | None = None

        cache: dict[Callable, Any] = {}
        self.state.cache = cache

        # Private mocks dict for testing - use via context manager
        self._mocks: dict[Callable, Callable] = {}

        # Configure log queue
        self.log_queue = LogQueue(sinks=log_sinks)
        self.router.on_startup.append(self.log_queue.start)
        self.router.on_shutdown.append(self.log_queue.stop)

    @property
    def routes(self) -> list[BaseRoute]:
        return self.router.routes

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        return self.router.url_path_for(name, **path_params)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope["app"] = self
        if self.middleware_stack is None:
            # Delegate to Starlette to build the middleware stack
            # - we like it so there's no need to re-implement it
            # Starlette expects its own instance; use the instance method to
            # construct the stack with our attributes
            self.middleware_stack = self.build_middleware_stack()
        await self.middleware_stack(scope, receive, send)

    def build_middleware_stack(self) -> ASGIApp:
        error_handler = None
        exception_handlers: dict[Any, ExceptionHandler] = {}

        for key, value in self.exception_handlers.items():
            if key in (500, Exception):
                error_handler = value
            else:
                exception_handlers[key] = value

        middleware = (
            [
                Middleware(
                    GuardianMiddleware, log_queue=self.log_queue, handler=error_handler
                )
            ]
            + self.user_middleware
            + [Middleware(ExceptionMiddleware, handlers=exception_handlers)]
        )

        app = self.router
        for cls, args, kwargs in reversed(middleware):
            app = cls(app, *args, **kwargs)
        return app

    def add(self, route: BaseRoute) -> None:
        # We diverge from Starlette here because I think having more control over
        #  the process of adding routes is more important for us in context of this library
        self.router.add(route)

    def mount(self, path: str, app: ASGIApp, name: str | None = None) -> None:
        self.router.mount(path, app=app, name=name)

    def host(self, host: str, app: ASGIApp, name: str | None = None) -> None:
        self.router.host(host, app=app, name=name)

    def add_middleware(
        self,
        middleware_class: _MiddlewareFactory[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        if self.middleware_stack is not None:
            raise MiddlewareError(
                "Cannot add middleware after the application has started",
                context={
                    "middleware_class": getattr(
                        middleware_class, "__name__", str(middleware_class)
                    ),
                },
                help_text="Middleware must be added during application initialization, before any requests are handled.",
                example="""from stario import Stario
from starlette.middleware.cors import CORSMiddleware

# ✅ Correct: Add middleware during initialization
app = Stario()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
)

# Or pass middleware in constructor:
app = Stario(
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"])
    ]
)

# ❌ Incorrect: Adding middleware after app started
# This will fail if app has already handled a request""",
            )
        self.user_middleware.insert(0, Middleware(middleware_class, *args, **kwargs))

    def add_exception_handler(
        self,
        exc_class_or_status_code: int | type[Exception],
        handler: ExceptionHandler,
    ) -> None:
        self.exception_handlers[exc_class_or_status_code] = handler

    def add_event_handler(
        self,
        event_type: str,
        func: Callable,
    ) -> None:
        self.router.add_event_handler(event_type, func)

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

    @contextmanager
    def mocks(self, overrides: dict[Any, Callable]) -> Generator[None, None, None]:
        """
        Context manager to temporarily override dependencies for testing.

        Handles unpacking Annotated types to extract the actual dependency function.
        This allows using Annotated declarations or direct functions as keys.

        Parameters:
            overrides: Dictionary mapping dependency functions to their mock replacements.
                      Keys can be either the dependency function directly or an Annotated type.

        Example:
            def mock_db() -> Database:
                return InMemoryDatabase()

            # Using the dependency function directly
            with app.mocks({get_db: mock_db}):
                with TestClient(app) as client:
                    resp = client.post("/api")

            # Or using Annotated declaration (also works)
            with app.mocks({Annotated[Database, get_db]: mock_db}):
                with TestClient(app) as client:
                    resp = client.post("/api")
        """
        # Unpack Annotated keys if needed
        unpacked = {
            _unpack_annotated_override_key(key): value
            for key, value in overrides.items()
        }

        # Apply overrides
        self._mocks.update(unpacked)
        try:
            yield
        finally:
            # Cleanup - remove only the keys we added
            for key in unpacked:
                self._mocks.pop(key, None)


def _unpack_annotated_override_key(key: Any) -> Callable:
    # First, unwrap generic type aliases (TypeAliasType, GenericAlias)
    nested = getattr(key, "__value__", None)
    if nested is not None:
        # It's a type alias, unwrap it to get the actual type
        if isinstance(key, TypeAliasType) or isinstance(key, types.GenericAlias):
            key = nested

    # Now check if we have an Annotated type
    if get_origin(key) is Annotated:
        args = get_args(key)
        if len(args) >= 2:
            # args[0] is the type, args[1] is the dependency function
            return args[1]

    return key
