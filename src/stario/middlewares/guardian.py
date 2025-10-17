import sys
import uuid

from starlette._utils import is_async_callable
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, ExceptionHandler, Message, Receive, Scope, Send

from stario.logging.loggers.access import AccessLogger
from stario.logging.queue import LogQueue


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
        log_queue: LogQueue,
        handler: ExceptionHandler | None = None,
    ) -> None:
        self.app = app
        self.handler = handler
        self.log_queue = log_queue
        self.access_logger = AccessLogger(log_queue)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # We need uid and duration
        request_id = str(uuid.uuid4())
        scope["stario.request_id"] = request_id
        exception_info = None

        # This is to track if the response has started
        #  borrowed from starlette
        response_started = False
        status_code = None

        async def _send(message: Message) -> None:
            nonlocal response_started, status_code, send

            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]

                # Update scope with useful information
                scope["stario.response_started"] = True
                message["headers"].append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )

            # Continue sending the message
            await send(message)

        self.access_logger.request(
            request_id=request_id,
            method=scope["method"],
            path=scope["path"],
            client=scope.get("client"),
        )

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            request = Request(scope)

            if self.handler is None:
                # Use our default 500 error handler.
                response = PlainTextResponse("Internal Server Error", status_code=500)

                exception_info = sys.exc_info()
                if exception_info == (None, None, None):
                    exception_info = None
                else:
                    # Ensure exc_info is properly typed
                    exception_info = (
                        exception_info if exception_info[0] is not None else None
                    )
            else:
                # Use an installed 500 error handler.
                if is_async_callable(self.handler):
                    # TODO: this needs a bit of typing magic fixes:
                    response = await self.handler(request, exc)  # type: ignore[assignment]
                else:
                    response = await run_in_threadpool(self.handler, request, exc)

            if not response_started and response is not None:
                # Response objects are callable ASGI applications
                await response(scope, receive, _send)

            # We always continue to raise the exception.
            # This allows servers to log the error, or allows test clients
            # to optionally raise the error within the test case.
            # raise exc

        # Log the response info
        self.access_logger.response(
            request_id=request_id,
            status_code=status_code or 500,
            exc_info=exception_info,
        )
