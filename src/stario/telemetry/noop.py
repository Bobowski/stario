"""No-op telemetry backend for high-throughput or benchmark runs."""

from types import TracebackType
from uuid import UUID

from .core import Attributes, Span, TelemetryStats
from .spans import NoOpSpan

_NOOP_SPAN_ID = UUID(int=0)
_NOOP_SPAN = NoOpSpan(_NOOP_SPAN_ID, _NOOP_SPAN_ID, None)


class NoOpTracer:
    """Tracer that discards all span data and performs no I/O.

    Every `create()` returns one shared `NoOpSpan` (same ids; parenting is a
    no-op). Use for benchmarks or when telemetry must stay out of the hot path.
    `stats()` is always zero.
    """

    __slots__ = ()

    def __enter__(self) -> NoOpTracer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass

    def create(
        self,
        name: str,
        attributes: Attributes | None = None,
        /,
        *,
        parent: Span | None = None,
    ) -> Span:
        return _NOOP_SPAN

    def on_end(self, span: Span) -> None:
        pass

    def stats(self) -> TelemetryStats:
        return TelemetryStats()


NOOP_TRACER = NoOpTracer()
