"""No-op telemetry backend for high-throughput or benchmark runs."""

from typing import Any, Self
from uuid import UUID

from .core import Span

_NOOP_SPAN_ID = UUID(int=0)


class NoOpTracer:
    """Tracer implementation that discards all span data and performs no I/O."""

    __slots__ = ()

    @classmethod
    def from_env(cls) -> Self:
        return cls()

    def __enter__(self) -> "NoOpTracer":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def create(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        /,
        *,
        parent_id: UUID | None = None,
    ) -> Span:
        return Span(_NOOP_SPAN_ID, self)

    def start(self, span_id: UUID) -> None:
        pass

    def set_attribute(self, span_id: UUID, name: str, value: Any) -> None:
        pass

    def set_attributes(self, span_id: UUID, attributes: dict[str, Any]) -> None:
        pass

    def add_event(
        self,
        span_id: UUID,
        name: str,
        attributes: dict[str, Any] | None = None,
        /,
        *,
        body: Any | None = None,
    ) -> None:
        pass

    def add_link(
        self,
        span_id: UUID,
        target_span_id: UUID,
        attributes: dict[str, Any] | None = None,
        /,
    ) -> None:
        pass

    def set_name(self, span_id: UUID, name: str) -> None:
        pass

    def fail(self, span_id: UUID, message: str) -> None:
        pass

    def end(self, span_id: UUID) -> None:
        pass
