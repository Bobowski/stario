"""
Single entrypoint for request handling above the router: error surface, tracing, and ``writer.end()`` guarantees.

The protocol dispatches a callable; this class is where policy lives so the route trie stays a pure match/registration
structure. ``create_task`` registers work the server can wait on during shutdown—use it instead of orphan ``asyncio.create_task`` calls for request-adjacent work.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from functools import lru_cache
from inspect import cleandoc
from typing import Any
from urllib.parse import urlencode

import stario.responses as responses
from stario.exceptions import HttpException, StarioError
from stario.http.context import Context, Middleware

from .router import Router, _normalize_path
from .writer import Writer

type UrlQueryScalar = str | int | float | bool
type UrlQueryValue = UrlQueryScalar | list[UrlQueryScalar] | tuple[UrlQueryScalar, ...]
type UrlQueryParams = Mapping[str, UrlQueryValue]
type ErrorHandler[E: Exception] = Callable[[Context, Writer, E], None | Awaitable[None]]

_app_logger = logging.getLogger("stario.http.app")


class App(Router):
    """Concrete app type: everything on ``Router`` plus errors, reverse URLs, and shutdown-aware tasks.

    Uncaught exceptions become HTTP responses only before headers are sent; after that, telemetry still records the failure.
    Use ``create_task`` for work tied to a running server so graceful shutdown can observe it.
    """

    def __init__(self, *, middleware: Sequence[Middleware] = ()) -> None:
        """Create an application (a ``Router`` with error handling and tasks) with optional router middleware.

        Parameters:
            middleware: Forwarded to the base ``Router``: wrappers around handlers registered on this app (see ``Router.push_middleware``).
        """
        super().__init__(middleware=middleware)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._shutdown: asyncio.Future[None] | None = None
        self._error_handlers: dict[type[Exception], ErrorHandler[Any]] = {
            HttpException: lambda c, w, exc: exc.respond(w),
        }

        @lru_cache(maxsize=64)
        def find_handler(exc_type: type[Exception]) -> ErrorHandler[Any] | None:
            # Most-specific registered type wins by walking the MRO.
            for t in exc_type.__mro__:
                if t is Exception:
                    return None
                if handler := self._error_handlers.get(t):
                    return handler
            return None

        self._find_error_handler = find_handler

    @property
    def shutting_down(self) -> bool:
        """``True`` once the server has started draining this app."""
        return self._shutdown is not None and self._shutdown.done()

    async def wait_shutdown(self) -> None:
        """Block until the server starts draining this app."""
        if self._shutdown is not None:
            await self._shutdown
            return
        raise StarioError(
            "app.wait_shutdown() requires a running server",
            help_text="Call app.wait_shutdown() from code running under Server, TestClient, or aload_app.",
        )

    def on_error(
        self, exc_type: type[Exception], handler: ErrorHandler[Exception]
    ) -> None:
        """Register a handler for uncaught exceptions of type ``exc_type`` (subclasses use MRO; most specific wins).

        Parameters:
            exc_type: Exception class to match.
            handler: Receives ``(context, writer, exc)``; may return ``None`` or an awaitable.

        Notes:
            Only runs while the writer has not started (``w.started`` is false); ``HttpException`` is registered by default.
        """
        self._error_handlers[exc_type] = handler
        self._find_error_handler.cache_clear()

    def create_task[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[T]:
        """Schedule a coroutine on the running loop and retain the task until it completes.

        Parameters:
            coro: Coroutine to run.
            name: Optional task name for debuggers.

        Returns:
            The new ``asyncio.Task``.

        Raises:
            StarioError: If no event loop is running (call from async request or app code only).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise StarioError(
                "app.create_task() requires a running event loop",
                help_text="Call app.create_task() from async code while the app is running.",
            ) from exc
        task = loop.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def join_tasks(self) -> None:
        """Await until every task created with ``create_task`` has finished (including nested scheduling).

        Useful in tests to wait for background work after the HTTP response has been sent. Do not call from
        inside a coroutine that is itself tracked in ``create_task`` or you risk deadlock.
        """
        while True:
            pending = set(self._tasks)
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)

    def url_for(
        self,
        name: str,
        *,
        params: dict[str, str] | None = None,
        query: UrlQueryParams | None = None,
    ) -> str:
        """Build a path (and optional query string) from a registered ``name``.

        Parameters:
            name: Value passed as ``name=`` to ``get``/``handle`` or from mounted ``StaticAssets``.
            params: ``str.format`` mapping for ``{placeholders}`` in the stored pattern.
            query: Values merged with ``urllib.parse.urlencode`` (lists/tuples repeat the key).

        Returns:
            A path starting with ``/``, or ``host/...`` style when the route used host matching, plus ``?...`` when ``query`` is set.

        Raises:
            StarioError: If ``name`` is unknown or placeholders are missing from ``params``.
        """
        path = self.named_routes.get(name)
        if path is None:
            raise StarioError(
                f"Reverse route not registered: '{name}'",
                context={
                    "name": name,
                    "available": list(self.named_routes.keys())[:10],
                },
                help_text=f"Register the route or asset first with name='{name}' before calling url_for().",
                example=cleandoc(
                    """
                    app.get("/", home, name="home")
                    app.mount("/static", StaticAssets("./static", name="static"))
                    """
                ),
            )

        if params:
            try:
                path = path.format(**params)
            except (KeyError, ValueError) as exc:
                raise StarioError(
                    f"url_for() could not fill placeholders in route '{name}'",
                    context={"name": name, "pattern": path, "params": params},
                    help_text="Ensure every `{name}` in the pattern has a matching key in params.",
                ) from exc

        if not query:
            return path
        return f"{path}?{urlencode(query, doseq=True)}"

    async def __call__(self, c: Context, w: Writer) -> None:
        """Protocol entrypoint: open a span, resolve routes, handle errors, always call ``w.end()``.

        Parameters:
            c: Request context (``app``, ``req``, ``span``, ``route``, ``state``).
            w: Response writer for this message on the connection.

        Notes:
            Trailing slashes (except ``/``) get ``308`` to the router-normalized path (same rules as
            ``find_handler``); wrong method on a matching path yields ``405``. If a registered error handler
            raises, the failure is logged and a 500 is sent when no response has started yet.
        """
        span = c.span
        span.start()
        span.attr("request.method", c.req.method)
        span.attr("request.path", c.req.path)

        try:
            path = c.req.path
            if path != "/" and path.endswith("/"):
                responses.redirect(w, _normalize_path(path), 308)
            else:
                handler, c.route = self._find_handler(c.req.host, path, c.req.method)
                await handler(c, w)
        except Exception as exc:
            # Error surface: try registered handlers; if they do not start a response, send 500.
            handler_responded = False
            if not w.started:
                if handler := self._find_error_handler(type(exc)):
                    try:
                        result = handler(c, w, exc)
                        if asyncio.iscoroutine(result):
                            await result
                        handler_responded = w.started
                    except Exception as handler_exc:
                        _app_logger.error(
                            "Error handler failed while handling %s (original: %r); "
                            "the handler should recover and send a response, but raised.",
                            type(exc).__name__,
                            exc,
                            exc_info=handler_exc,
                        )
                if not w.started:
                    responses.text(w, "Internal Server Error", 500)
            if not handler_responded:
                span.fail(str(exc))
                span.exception(exc)
        finally:
            # ``HttpProtocol`` completion / keep-alive depends on ``end()``.
            w.end()
            span.attr("response.status_code", w.status_code)
            span.end()
