from typing import Any, AsyncGenerator, Callable, Generator, Protocol

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse

# from stario.application import Stario
from stario.datastar import _HtmlProvider, patch_to_sse
from stario.dependencies import Dependency
from stario.types import CacheScope, RunMode


class HandlerWrapper(Protocol):

    def __init__(self, handler: Callable, **kwargs: Any): ...

    async def __call__(self, request: Request) -> Response: ...


class QuickRouteWrapper:

    def __init__(
        self,
        handler: Callable,
        *,
        cache: CacheScope = "request",
        mode: RunMode = "auto",
    ) -> None:
        self.handler = handler
        self.dep = Dependency.build(handler, cache, mode)

    async def __call__(self, request: Request) -> Response:

        app = request.app

        content = await self.dep.resolve(request, app, {})

        if isinstance(content, Response):
            return content

        if isinstance(content, Generator):
            # fmt: off
            return StreamingResponse(
                content    = (patch_to_sse(item) for item in content),
                media_type = "text/event-stream",
                headers = {
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            # fmt: on

        if isinstance(content, AsyncGenerator):
            # fmt: off
            return StreamingResponse(
                content    = (patch_to_sse(item) async for item in content),
                media_type = "text/event-stream",
                headers = {
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            # fmt: on

        if isinstance(content, _HtmlProvider):
            content = content.__html__()

        return HTMLResponse(
            content,
            status_code=200 if content else 204,
        )
