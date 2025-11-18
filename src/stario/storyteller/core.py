import time
from contextlib import contextmanager
from typing import Annotated, Any, Iterable

from stario.requests import Request

from .types import LogRecord, StoryListener


class Storyteller:
    """
    Storyteller is the place where all the logging happens.
    Then it's passed to all initialized listeners.
    """

    __slots__ = ("listeners", "ctx", "_single_listener")

    def __init__(self, listeners: Iterable[StoryListener], **kwargs: Any):
        self.listeners = list(listeners)
        self.ctx = kwargs
        # Cache single sink for fast path (~16% faster for common single-sink case)
        self._single_listener: StoryListener | None = (
            self.listeners[0] if len(self.listeners) == 1 else None
        )

    def enqueue(self, record: LogRecord) -> None:
        if self._single_listener is not None:
            # Fast path: single sink, direct call (no iteration overhead)
            return self._single_listener.enqueue(record)

        # Multiple sinks: iterate
        for listener in self.listeners:
            listener.enqueue(record)

    def bind(self, **kwargs: Any) -> "Storyteller":
        """
        Bind context to the storyteller instance. Returns a new storyteller instance with the bound context.
        """
        return Storyteller(self.listeners, **self.ctx, **kwargs)

    def open(self) -> None:
        """Open the storyteller."""
        for listener in self.listeners:
            listener.open()

    def close(self) -> None:
        """Close the storyteller."""
        for listener in self.listeners:
            listener.close()

    def tell(self, event: str, **kwargs: Any) -> None:
        self.enqueue(
            {
                "time_ns": time.time_ns(),
                "event": event,
                **self.ctx,
                **kwargs,
            }
        )

    def failure(
        self,
        event: str,
        *,
        exc: BaseException | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Log a backend or system failure (unexpected/internal errors, crashes, etc).
        """
        self.tell(event, exc=exc, error="system", reason=reason, **kwargs)

    def misstep(
        self,
        event: str,
        *,
        reason: str | None = None,
        exc: BaseException | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Log a user-facing or expected misstep (validation errors, user action errors, etc).
        """
        self.tell(event, exc=exc, error="user", reason=reason, **kwargs)

    @contextmanager
    def timed(
        self,
        event: str,
        key_name: str = "duration_ns",
        **kwargs: Any,
    ):
        """
        Context manager to measure and log elapsed time of a code block.
        """
        start_ns = time.perf_counter_ns()
        try:
            yield
        finally:
            kwargs[key_name] = time.perf_counter_ns() - start_ns
            self.tell(event, **kwargs)


def get_request_storyteller(request: Request) -> Storyteller:
    """
    Returns the per-request Storyteller instance, bound with trace/context info and memoized on the ASGI scope.
    """
    return request.scope["stario.request_storyteller"]


type RequestStoryteller = Annotated[Storyteller, get_request_storyteller]
