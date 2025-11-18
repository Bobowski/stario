import asyncio
import json
import sys
import traceback

from stario.storyteller.types import LogRecord

type JsonValue = str | int | float | bool | dict[str, JsonValue] | list[JsonValue]
type JsonDict = dict[str, JsonValue]


def json_dumps(log: LogRecord) -> str:

    if exc := log.pop("exc", None):

        exc_type = type(exc)
        log["exc_type"] = exc_type.__name__
        log["exc_msg"] = str(exc)

        if tb := getattr(exc, "__traceback__", None):
            log["exc_stack"] = traceback.format_exception(exc_type, exc, tb)

    return json.dumps(log, default=str, separators=(",", ":"), ensure_ascii=False)


class JsonListener:
    """JSON output listener for structured logging."""

    def __init__(
        self,
        buffer_size: int = 100,
        flush_interval: float = 0.05,
        stacktrace_limit: int = 5,
    ):
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self._buffer: list[LogRecord] = []
        self._flush_task: asyncio.Task | None = None

    def open(self) -> None:
        """Start the buffer flusher task."""
        self._flush_task = asyncio.create_task(self._buffer_flusher())

    def close(self) -> None:
        """Stop the buffer flusher task."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None

    def enqueue(self, record: LogRecord) -> None:
        """Enqueue a log record."""
        self._buffer.append(record)

    async def _buffer_flusher(self) -> None:
        """Periodically flush buffered records."""
        while True:
            try:
                if self._buffer:
                    records, self._buffer = self._buffer, []
                    await self.write(records)
                await asyncio.sleep(self.flush_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in buffer flusher: {type(e).__name__}: {e}")
                await asyncio.sleep(self.flush_interval)

    async def write(self, records: list[LogRecord]) -> None:
        """Write records to stdout as newline-delimited JSON."""
        if not records:
            return

        sys.stdout.writelines((json_dumps(r) + "\n" for r in records))
        sys.stdout.flush()

