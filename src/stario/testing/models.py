"""Request/response and telemetry snapshot types for the test client."""

import json as json_module
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from stario.http.headers import Headers


@dataclass(slots=True, frozen=True)
class TelemetryEvent:
    name: str
    time_ns: int
    attributes: dict[str, Any] = field(default_factory=lambda: {})
    body: Any | None = None


@dataclass(slots=True, frozen=True)
class TelemetryLink:
    name: str
    span_id: UUID
    attributes: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(slots=True, frozen=True)
class TelemetrySpan:
    id: UUID
    name: str
    parent_id: UUID | None
    start_ns: int
    end_ns: int
    status: str
    error: str | None
    attributes: dict[str, Any] = field(default_factory=lambda: {})
    events: tuple[TelemetryEvent, ...] = ()
    links: tuple[TelemetryLink, ...] = ()

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True, frozen=True)
class ClientRequest:
    method: str
    url: str
    path: str
    query_string: str
    headers: Headers
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json_module.loads(self.content)
