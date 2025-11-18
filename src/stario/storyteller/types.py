from typing import Any, Protocol

# class LogRecord(TypedDict):
#     """Base log entry."""

#     time_ns: int  # timestamp in nanoseconds since epoch (time.time_ns())
#     trace_id: str  # UUIDv7 correlation ID (uuid.uuid7())
#     event: str  # event type: "request_started", "response_started", "response_completed" etc


type LogRecord = dict[str, Any]


class StoryListener(Protocol):
    """Protocol for log output destinations."""

    def open(self) -> None:
        """Open the sink."""
        ...

    def close(self) -> None:
        """Close the sink."""
        ...

    def enqueue(self, record: LogRecord) -> None:
        """Enqueue a log record to the sink."""
        ...
