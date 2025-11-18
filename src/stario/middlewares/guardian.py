import uuid

from starlette._utils import is_async_callable
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, ExceptionHandler, Message, Receive, Scope, Send

from stario.storyteller import Storyteller


class GuardianMiddleware:
    """
    Handles returning 500 responses when a server error occurs.

    If 'debug' is set, then traceback responses will be returned,
    otherwise the designated 'handler' will be called.

    This middleware class should generally be used to wrap *everything*
    else up, so that unhandled exceptions anywhere in the stack
    always result in an appropriate 500 response.

    Based on starlette.middleware.exceptions.ExceptionMiddleware
    """

    def __init__(
        self,
        app: ASGIApp,
        app_storyteller: Storyteller,
        handler: ExceptionHandler | None = None,
    ) -> None:
        self.app = app
        self.app_storyteller = app_storyteller
        self.handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # We need uid and duration
        trace_id = str(uuid.uuid7())
        # Create a request-specific storyteller (bound with trace_id)
        story = self.app_storyteller.bind(trace_id=trace_id)
        scope["stario.request_storyteller"] = story

        story.tell(
            "request.start",
            method=scope.get("method"),
            path=scope.get("path"),
            client=scope.get("client"),
        )

        # This is to track if the response has started (borrowed from starlette)
        response_started = False
        status_code = None

        async def _send(message: Message) -> None:
            nonlocal response_started, status_code

            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]

                # Update scope with useful information
                message["headers"].append((b"x-request-id", trace_id.encode("latin-1")))

                story.tell("response.start", status_code=status_code)

            await send(message)

            # Log when we send the last part of the body
            if (
                message["type"] == "http.response.body"
                and message.get("more_body") is False
            ):
                story.tell("response.end")

        try:
            await self.app(scope, receive, _send)

        except Exception as exc:

            if self.handler is None:
                # This is the default 500 error handler :)
                response = PlainTextResponse("Internal Server Error", status_code=500)
                story.failure("request.error", exc=exc, reason="Unexpected error")
            else:
                request = Request(scope)

                # Use an installed 500 error handler.
                if is_async_callable(self.handler):
                    # ExceptionHandler can be async or sync, and returns Response | None
                    # We handle both cases appropriately
                    response = await self.handler(request, exc)  # type: ignore[assignment]
                else:
                    response = await run_in_threadpool(self.handler, request, exc)

            if not response_started and response is not None:
                # Response objects are callable ASGI applications
                await response(scope, receive, _send)

        # Log the response info
        story.tell("request.end")
