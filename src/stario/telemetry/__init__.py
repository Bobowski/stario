"""
Telemetry sinks and span types for Stario apps.

Handlers see `Span` on `Context`. Import tracer backends from their modules
when wiring a process:

```python
from stario.telemetry.json import JsonTracer
from stario.telemetry.tty import TTYTracer
from stario.telemetry.noop import NoOpTracer
from stario.telemetry.sqlite import SqliteTracer
```

`stario.serve` reads `STARIO_TRACER` via `tracer_from_env()` when no
tracer is passed.
"""

from .core import EventBody, Span, TelemetryStats, Tracer
from .env import tracer_from_env
from .spans import NoOpSpan, ProxySpan, RecordedEvent, RecordedLink, RecordingSpan

__all__ = [
    "EventBody",
    "NoOpSpan",
    "ProxySpan",
    "RecordedEvent",
    "RecordedLink",
    "RecordingSpan",
    "Span",
    "TelemetryStats",
    "Tracer",
    "tracer_from_env",
]
