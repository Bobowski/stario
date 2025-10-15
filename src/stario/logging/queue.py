import asyncio
import sys
from typing import Iterable

from stario.logging.sinks.json import JSONSink
from stario.logging.sinks.rich import RichConsoleSink
from stario.logging.types import (
    Record,
    Sink,
)


class LogQueue:
    """
    Log queue that handles buffering and flushing to sinks.

    Uses a listener pattern where loggers call enqueue() which handles
    buffering and delayed flushing via asyncio tasks.
    """

    def __init__(
        self,
        sinks: Iterable[Sink] | None = None,
        buffer_size: int = 100,
        flush_interval: float = 0.05,
    ):

        if sinks is None:
            if sys.stdout.isatty():
                self.sinks = [RichConsoleSink()]
            else:
                self.sinks = [JSONSink()]
        else:
            self.sinks = list(sinks)

        self.buffer_size = buffer_size
        self.flush_interval = flush_interval

        self._buffer: list[Record] = []
        self._flush_task: asyncio.Task | None = None
        self._running = False

    def enqueue(self, record: Record) -> None:
        """
        Enqueue a log record for writing.

        Implements throttled buffering:
        - If buffer full: flush immediately
        - Otherwise: buffer and ensure flush task is running
        - Flush task runs at regular intervals (throttling, not debouncing)
        """
        self._buffer.append(record)

        # If buffer is full, schedule immediate flush
        if len(self._buffer) >= self.buffer_size:
            if self._flush_task and not self._flush_task.done():
                # Cancel current flush task and create new one
                self._flush_task.cancel()
            self._flush_task = asyncio.create_task(self._flush_buffer())

    async def _buffer_flusher(self) -> None:
        """
        Flush the buffer to all sinks at regular intervals.
        """
        try:
            while self._running:
                await self._flush_buffer()

                await asyncio.sleep(self.flush_interval)

            # Flush the buffer one last time before stopping
            await self._flush_buffer()

            for sink in self.sinks:
                sink.close()

        except Exception as e:
            print(f"Error in buffer flusher: {type(e).__name__}: {str(e)}")
            raise

    async def _flush_buffer(self) -> None:
        """Flush the current buffer to all sinks."""
        if not self._buffer:
            return

        # Swap buffers
        records = self._buffer
        self._buffer = []

        # Write to all sinks; if only one, do it directly for efficiency
        if len(self.sinks) == 1:
            await self.sinks[0].write(records)

        else:
            await asyncio.gather(
                *[sink.write(records) for sink in self.sinks],
                return_exceptions=True,
            )

    def start(self) -> None:
        """Start the log queue."""
        self._running = True
        self._flush_task = asyncio.create_task(self._buffer_flusher())

        for sink in self.sinks:
            sink.open()

    def stop(self) -> None:
        """Stop and flush remaining logs."""
        self._running = False
