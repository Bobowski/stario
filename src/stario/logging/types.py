from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol


class LogLevel(IntEnum):
    """Log levels with numeric values for comparison."""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


@dataclass(slots=True)
class BaseLogRecord:
    """Base log entry."""

    timestamp: float  # UTC timestamp
    request_id: str  # UUIDv4 correlation ID


@dataclass(slots=True)
class RouteLogRecord(BaseLogRecord):
    """Application/debug log entry."""

    level: LogLevel
    message: str
    context: dict[str, Any]
    buffered: bool = False  # Sent only if log level is above threshold
    background: bool = False  # Sent from background tasks (after response has started)
    exc_info: tuple[type, BaseException, Any] | None = None


@dataclass(slots=True)
class RequestLogRecord(BaseLogRecord):
    """HTTP request log entry."""

    method: str
    path: str
    client: tuple[str, int] | None  # (host, port)


@dataclass(slots=True)
class ResponseLogRecord(BaseLogRecord):
    """HTTP response log entry."""

    status_code: int
    exc_info: tuple[type, BaseException, Any] | None


# Union type for all log records
Record = RequestLogRecord | ResponseLogRecord | RouteLogRecord


class Sink(Protocol):
    """Protocol for log output destinations."""

    def open(self) -> None:
        """Open the sink."""
        ...

    def close(self) -> None:
        """Close the sink."""
        ...

    async def write(self, records: list[Record]) -> None:
        """Write one or more log records to the sink."""
        ...


class LogQueue(Protocol):
    """Protocol for a log queue."""

    def enqueue(self, record: Record) -> None: ...
