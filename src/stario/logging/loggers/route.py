import sys
import time
from contextlib import contextmanager
from typing import Annotated, Any

from starlette.types import Scope

from stario.logging.types import LogLevel, LogQueue, RouteLogRecord
from stario.requests import Request


class RouteLogger:
    """
    Threshold-based buffered logger for request/response lifecycle.

    Buffers logs until a message at or above threshold_level is logged.
    When triggered:
    1. Flushes all buffered messages (marked with buffered=True)
    2. Sends the trigger message
    3. Switches to pass-through mode (all subsequent logs sent immediately)
    """

    __slots__ = (
        "queue",
        "min_level",
        "threshold_level",
        "request_id",
        "scope",
        "context",
        "buffer",
        "triggered",
    )

    def __init__(
        self,
        # Configuration
        queue: LogQueue,
        min_level: LogLevel,
        threshold_level: LogLevel,
        # Context
        scope: Scope,
    ):

        # Configuration
        self.queue = queue
        self.min_level = min_level
        self.threshold_level = threshold_level

        # Context
        self.request_id = scope["stario.request_id"]
        self.scope = scope

        # State
        self.buffer: list[RouteLogRecord] = []
        self.triggered = min_level >= threshold_level

    def _log(
        self,
        level: LogLevel,
        message: str,
        exc_info: tuple[type, BaseException, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Internal logging method."""
        if level < self.min_level:
            return

        record = RouteLogRecord(
            request_id=self.request_id,
            timestamp=time.time(),
            level=level,
            message=message,
            context=kwargs,
            buffered=False,
            background=self.scope.get("stario.response_started", False),
            exc_info=exc_info,
        )

        # Fast path: Passthrough mode
        if self.triggered:
            self.queue.enqueue(record)
            return

        # Buffer mode: Possibly trigger threshold
        self.buffer.append(record)

        # When we keep buffering...
        if level < self.threshold_level:
            return

        # When we trigger the threshold...
        self.triggered = True
        for record in self.buffer:
            record.buffered = True
            self.queue.enqueue(record)
        self.buffer.clear()

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log a debug message."""
        self._log(LogLevel.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """Log an info message."""
        self._log(LogLevel.INFO, message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log a warning message."""
        self._log(LogLevel.WARNING, message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Log an error message."""
        self._log(LogLevel.ERROR, message, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        """Log a critical message."""
        self._log(LogLevel.CRITICAL, message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        """Log an exception with traceback."""
        exc_info = sys.exc_info()
        if exc_info == (None, None, None):
            exc_info = None
        else:
            # Ensure exc_info is properly typed
            exc_info = exc_info if exc_info[0] is not None else None
        self._log(LogLevel.ERROR, message, exc_info=exc_info, **kwargs)

    def flush(self) -> None:
        """
        Manually flush buffered logs and switch to pass-through mode.

        Buffered messages are marked with buffered=True.
        """
        self.triggered = True

        if len(self.buffer) == 0:
            return

        # Mark and send all buffered records
        for record in self.buffer:
            record.buffered = True
            self.queue.enqueue(record)

        # Clear and switch to pass-through mode
        self.buffer.clear()

    def discard(self) -> None:
        """Discard buffered logs without writing."""
        self.buffer.clear()

    @contextmanager
    def timed(
        self,
        message: str,
        level=LogLevel.INFO,
        duration_key: str = "duration",
        **kwargs: Any,
    ):
        """
        Context manager to measure and log elapsed time of a code block.
        """
        start = time.perf_counter()
        yield
        self._log(
            level,
            message,
            **{duration_key: time.perf_counter() - start},
            **kwargs,
        )


def get_route_logger(request: Request) -> RouteLogger:

    return RouteLogger(
        queue=request.app.log_queue,
        min_level=LogLevel.INFO,
        threshold_level=LogLevel.INFO,
        scope=request.scope,
    )


Logger = Annotated[RouteLogger, get_route_logger]
