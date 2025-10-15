from dataclasses import dataclass
from typing import AsyncGenerator, Awaitable, Callable, ClassVar, Generator

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from stario.dependencies import Dependency, DependencyLifetime
from stario.html.core import render

from .events import patch_to_sse

type EndpointFunction[T] = Callable[..., T]
type RequestHandler = Callable[[Scope, Receive, Send], Awaitable[None]]
type AdapterFunction[T] = Callable[[EndpointFunction[T]], RequestHandler]


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

        # Resolve dependencies
        content = await self.dependencies.resolve(request)

        # Fast path: direct Response return (most common case)
        if isinstance(content, Response):
            return await content(scope, receive, send)

        # Fast path: Generator (SSE streaming)
        if isinstance(content, Generator):
            response = StreamingResponse(
                content=(patch_to_sse(item, self.renderer) for item in content),
                media_type="text/event-stream",
                headers=self.SSE_HEADERS,
            )
            return await response(scope, receive, send)

        # Fast path: AsyncGenerator (SSE streaming)
        if isinstance(content, AsyncGenerator):
            response = StreamingResponse(
                content=(patch_to_sse(item, self.renderer) async for item in content),
                media_type="text/event-stream",
                headers=self.SSE_HEADERS,
            )
            return await response(scope, receive, send)

        # Fast path: None content
        # TODO: should we assume not None or we are ok with bool()? think lists, strings, etc.
        if not content:
            response = HTMLResponse(content=None, status_code=204)
            return await response(scope, receive, send)

        # Render content and return HTML response
        rendered = self.renderer(content)
        response = HTMLResponse(content=rendered, status_code=200)
        return await response(scope, receive, send)


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

    def decorator(handler: EndpointFunction[T]) -> RequestHandler:
        """
        Convert a handler function into a Starlette-compatible request handler.

        The handler function will receive resolved dependencies as arguments
        and can return various response types that will be automatically processed.
        """

        # Dependencies can be build once and just used on every request
        dependencies = Dependency.build(handler, lifetime)

        return _StarioAdapter(dependencies, renderer)

    return decorator
