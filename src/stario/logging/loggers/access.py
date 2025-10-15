import time
from typing import Any

from stario.logging.queue import LogQueue
from stario.logging.types import (
    LogLevel,
    RequestLogRecord,
    ResponseLogRecord,
)


class AccessLogger:
    """
    Logger for HTTP access logs (requests and responses).
    """

    def __init__(
        self,
        queue: LogQueue,
        default_level: LogLevel = LogLevel.INFO,
    ):
        self.queue = queue
        self.default_level = default_level

    def request(
        self,
        request_id: str,
        method: str,
        path: str,
        client: tuple[str, int] | None = None,
    ) -> None:
        self.queue.enqueue(
            RequestLogRecord(
                timestamp=time.time(),
                request_id=request_id,
                method=method,
                path=path,
                client=client,
            )
        )

    def response(
        self,
        request_id: str,
        status_code: int,
        exc_info: tuple[type, BaseException, Any] | None = None,
    ) -> None:
        self.queue.enqueue(
            ResponseLogRecord(
                timestamp=time.time(),
                request_id=request_id,
                status_code=status_code,
                exc_info=exc_info,
            )
        )
