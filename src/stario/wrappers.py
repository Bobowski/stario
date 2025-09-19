from typing import Any, AsyncGenerator, Callable, Generator, Protocol

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse

# from stario.application import Stario
from stario.datastar import _HtmlProvider, patch_to_sse
from stario.dependencies import Dependency


class HandlerWrapper(Protocol):

    def __init__(self, handler: Callable, **kwargs: Any): ...

    async def __call__(self, request: Request) -> Response: ...


class QuickRouteWrapper:

    def __init__(self, handler: Callable) -> None:
        self.handler = handler
        self.dep = Dependency.build(handler)

    async def __call__(self, request: Request) -> Response:

        app = request.app

        content = await self.dep.resolve(app, request)

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
