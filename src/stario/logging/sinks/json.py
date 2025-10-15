import json
import sys
import traceback
from datetime import datetime, timezone

from stario.logging.types import (
    Record,
    RequestLogRecord,
    ResponseLogRecord,
    RouteLogRecord,
)

type JsonValue = str | int | float | bool | dict[str, JsonValue] | list[JsonValue]
type JsonDict = dict[str, JsonValue]


class JSONSink:
    """Optimized JSON output for production/structured logging."""

    def __init__(self):
        # Pre-compute timezone for performance
        self._utc = timezone.utc

    def open(self) -> None:
        """Open the sink."""
        pass

    def close(self) -> None:
        """Close the sink."""
        pass

    async def write(self, records: list[Record]) -> None:
        """Write log records as JSON lines with minimal overhead."""
        if not records:
            return

        # Pre-allocate list to avoid repeated list comprehensions
        lines = []
        for record in records:
            lines.append(self._record_to_json(record))
            lines.append("\n")

        # Single write operation for better I/O performance
        sys.stdout.write("".join(lines))
        sys.stdout.flush()

    def _record_to_json(self, record: Record) -> str:
        """Convert record to JSON string with optimized performance."""
        # Handle RequestLogRecord
        if isinstance(record, RequestLogRecord):
            data: JsonDict = {
                "timestamp": datetime.fromtimestamp(
                    record.timestamp, tz=self._utc
                ).isoformat(),
                "type": "request",
                "method": record.method,
                "path": record.path,
                "request_id": record.request_id,
            }
            if record.client:
                data["client"] = {
                    "host": record.client[0],
                    "port": record.client[1],
                }
            return json.dumps(data, separators=(",", ":"), default=str)

        # Handle ResponseLogRecord
        elif isinstance(record, ResponseLogRecord):
            data: JsonDict = {
                "timestamp": datetime.fromtimestamp(
                    record.timestamp, tz=self._utc
                ).isoformat(),
                "type": "response",
                "status_code": record.status_code,
                "request_id": record.request_id,
            }
            if record.exc_info:
                data["exception"] = self._format_exception(record.exc_info)
            return json.dumps(data, separators=(",", ":"), default=str)

        # Handle RouteLogRecord
        elif isinstance(record, RouteLogRecord):
            data: JsonDict = {
                "timestamp": datetime.fromtimestamp(
                    record.timestamp, tz=self._utc
                ).isoformat(),
                "type": "log",
                "level": record.level.name,
                "message": record.message,
                "buffered": record.buffered,
                "background": record.background,
                "request_id": record.request_id,
                "context": record.context,
            }
            if record.exc_info:
                data["exception"] = self._format_exception(record.exc_info)
            return json.dumps(data, separators=(",", ":"), default=str)

        # Fallback for unknown record types (should be rare)
        return json.dumps(
            {"error": "Unknown record type", "record": str(record)},
            separators=(",", ":"),
            default=str,
        )

    def _format_exception(self, exc_info: tuple) -> JsonDict:
        """Format exception info for JSON output with minimal overhead."""
        # Pre-allocate dict with known size
        result = {
            "type": exc_info[0].__name__,
            "message": str(exc_info[1]),
        }

        # Only format traceback if needed (expensive operation)
        if exc_info[2] is not None:
            result["traceback"] = "".join(traceback.format_tb(exc_info[2]))

        return result
