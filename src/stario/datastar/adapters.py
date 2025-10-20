import asyncio
from dataclasses import dataclass
from typing import AsyncGenerator, Callable, ClassVar, Generator

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from stario.dependencies import Dependency, DependencyLifetime
from stario.html.core import render
from stario.types import AdapterFunction, EndpointFunction, RequestHandler

from .events import patch_to_sse


@dataclass(slots=True)
class _StarioAdapter:
    """
    High-performance request adapter that converts handler functions into ASGI-compatible request handlers.

    This adapter handles dependency injection, response type detection, and content rendering
    with optimized fast paths for common response patterns.
    """

    dependencies: Dependency
    renderer: Callable[..., str]

    SSE_HEADERS: ClassVar[dict[str, str]] = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Process a request through the dependency injection system and return an appropriate response.

        Handles multiple response types:
        - Direct Response objects (passed through unchanged)
        - Generators/AsyncGenerators (converted to SSE streams)
        - Other content (rendered to HTML)
        - None (returns 204 No Content)
        """

        # Create request for easier access
        request = Request(scope, receive, send)

        try:
            # Resolve dependencies
            content = await self.dependencies.resolve(request)

            # Fast path: direct Response return (most common case)
            if isinstance(content, Response):
                response = content

            # Fast path: Generator (SSE streaming)
            elif isinstance(content, Generator):
                response = StreamingResponse(
                    content=(patch_to_sse(item, self.renderer) for item in content),
                    media_type="text/event-stream",
                    headers=self.SSE_HEADERS,
                )

            # Fast path: AsyncGenerator (SSE streaming)
            elif isinstance(content, AsyncGenerator):
                response = StreamingResponse(
                    content=(
                        patch_to_sse(item, self.renderer) async for item in content
                    ),
                    media_type="text/event-stream",
                    headers=self.SSE_HEADERS,
                )

            # Fast path: None content
            elif content is None:
                response = HTMLResponse(content=None, status_code=204)

            # Render content and return HTML response
            else:
                rendered = self.renderer(content)
                response = HTMLResponse(content=rendered, status_code=200)

            # Send response
            await response(scope, receive, send)

        finally:

            # Cleanup is guaranteed to run here, even on exception or early return
            # Reverse order to ensure LIFO cleanup (last dependency in, first out)
            for cleanup in reversed(getattr(request.state, "cleanups", [])):
                try:
                    if cleanup[0]:
                        await cleanup[1](None, None, None)
                    else:
                        cleanup[1](None, None, None)
                except Exception:
                    # Log cleanup exceptions but don't interrupt other cleanups
                    continue


@dataclass(slots=True)
class _DetachedCommandAdapter:
    """
    Adapter for fire-and-forget operations that resolves all dependencies during request handling
    and delegates the handler execution to a background task.

    Returns 204 No Content immediately if all dependencies resolve successfully.
    Returns error response if any dependency fails.
    """

    dependencies: Dependency
    renderer: Callable[..., str]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Process a request by resolving all dependencies and delegating handler to background.

        Flow:
        1. Resolve all dependencies while request is being processed
        2. If successful, schedule handler as background task and return 204
        3. If dependency resolution fails, return error response (from exception)
        """

        # Create request for easier access
        request = Request(scope, receive, send)

        try:
            if len(self.dependencies.children) == 0:
                arguments = {}

            elif len(self.dependencies.children) == 1:
                # Single child - no need for TaskGroup
                child = self.dependencies.children[0]
                arguments = {child.name: await child.resolve(request)}

            else:
                # Multiple children - use gather for parallel execution
                results = await asyncio.gather(
                    *[d.resolve(request) for d in self.dependencies.children],
                    return_exceptions=True,
                )
                # Check for exceptions and raise the first one found
                for result in results:
                    if isinstance(result, Exception):
                        raise result
                arguments = {
                    c.name: result
                    for c, result in zip(self.dependencies.children, results)
                }

            # We know that no exceptions were raised, so we can create the background task
            response = HTMLResponse(
                content=None,
                status_code=204,
                background=BackgroundTask(self.dependencies.function, **arguments),
            )
            await response(scope, receive, send)

        finally:

            # Cleanup is guaranteed to run here, even on exception or early return
            # Reverse order to ensure LIFO cleanup (last dependency in, first out)
            for cleanup in reversed(getattr(request.state, "cleanups", [])):
                try:
                    if cleanup[0]:
                        await cleanup[1](None, None, None)
                    else:
                        cleanup[1](None, None, None)
                except Exception:
                    # Log cleanup exceptions but don't interrupt other cleanups
                    continue


def handler[T](
    lifetime: DependencyLifetime = "request",
    renderer: Callable[..., str] = render,
) -> AdapterFunction[T]:
    """
    This decorator factory returns decorator that is able to convert functions into request handlers.
    We apply dependency injection and response processing to the handler function.

    def foo() -> str:
        return "foo"

    decorator = handler()

    request_handler = decorator(foo)
    # request_handler is now a Starlette-compatible request foo(Request) -> Response

    """

    def decorator(handler_func: EndpointFunction[T]) -> RequestHandler:
        """
        Convert a handler function into a Starlette-compatible request handler.

        The handler function will receive resolved dependencies as arguments
        and can return various response types that will be automatically processed.
        """

        # Dependencies can be build once and just used on every request
        dependencies = Dependency.build(handler_func, lifetime)

        return _StarioAdapter(dependencies, renderer)

    return decorator


def detached_command[T](
    lifetime: DependencyLifetime = "request",
    renderer: Callable[..., str] = render,
) -> AdapterFunction[T]:
    """
    Decorator for fire-and-forget commands that resolves dependencies during request handling
    and delegates handler execution to a background task.

    Returns 204 No Content immediately if all dependencies resolve successfully.
    Returns error response if any dependency fails.

    This is useful for:
    - Long-running operations that don't need to block the response
    - Background jobs triggered by HTTP requests
    - Operations where you want to validate dependencies but not wait for execution

    Example:

        @app.post("/send-email", detached_command())
        async def send_email(user_id: PathParam[int], mailer: Annotated[Mailer, get_mailer]):
            # Dependencies are resolved before this runs
            # Handler executes in background
            # User gets 204 immediately
            await mailer.send_welcome_email(user_id)

    """

    def decorator(handler_func: EndpointFunction[T]) -> RequestHandler:
        """
        Convert a handler function into a detached-execution request handler.

        The handler function's dependencies are resolved during request processing,
        but the handler itself runs in a background task after 204 is returned.
        """

        # Dependencies can be built once and just used on every request
        dependencies = Dependency.build(handler_func, lifetime)

        return _DetachedCommandAdapter(dependencies, renderer)

    return decorator
