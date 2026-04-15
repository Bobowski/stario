"""
Process-local pub/sub with dotted subjects: enough for fan-out between handlers and tasks in one interpreter.

``publish`` is synchronous and thread-safe so non-async code can signal asyncio subscribers; scope stops at process
boundary—use an external broker when you need cross-machine delivery or persistence guarantees.
"""

from asyncio import AbstractEventLoop, Queue, get_running_loop
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Lock
from types import TracebackType
from typing import Any, Self

# Message: (subject, data)
type Msg[T = Any] = tuple[str, T]

# Subscriber: (queue, loop) — loop is where ``call_soon_threadsafe`` must schedule puts
type _Sub[T] = tuple[Queue[Msg[T]], AbstractEventLoop]


def _no_running_event_loop(exc: RuntimeError) -> bool:
    """True when ``get_running_loop()`` failed because this thread has no loop."""
    return "no running event loop" in str(exc).lower()


def _closed_loop_enqueue_error(exc: RuntimeError) -> bool:
    """True when enqueue failed because the subscriber's loop is closed (shutdown)."""
    msg = str(exc).lower()
    if "event loop is closed" in msg:
        return True
    if "cannot schedule" in msg and "closed" in msg:
        return True
    return False


@lru_cache(maxsize=1024)
def _matching_patterns(subject: str) -> tuple[str, ...]:
    """
    Generate all patterns that would match this subject.

    "room.123.moves" -> ("room.123.moves", "room.123.*", "room.*", "*")
    """
    parts = subject.split(".")
    patterns = [subject]
    for i in range(len(parts) - 1, 0, -1):
        patterns.append(".".join(parts[:i]) + ".*")
    patterns.append("*")
    return tuple(patterns)


@dataclass(slots=True)
class RelaySubscription[T = Any]:
    """Registered queue on a ``Relay``: ``async with`` then ``async for``, or ``async for`` alone (auto enter/exit for the loop)."""

    _relay: Relay[T]
    pattern: str
    _queue: Queue[Msg[T]] = field(default_factory=Queue, init=False, repr=False)
    # Set while registered in ``Relay._subs``; used as (queue, loop) identity for removal
    _entry: _Sub[T] | None = field(default=None, init=False, repr=False)

    def _register(self) -> None:
        if self._entry is not None:
            raise RuntimeError("RelaySubscription is already active")
        loop = get_running_loop()
        entry: _Sub[T] = (self._queue, loop)
        self._entry = entry
        with self._relay._lock:
            if self.pattern not in self._relay._subs:
                self._relay._subs[self.pattern] = []
            self._relay._subs[self.pattern].append(entry)

    def _unregister(self) -> None:
        entry = self._entry
        if entry is None:
            return
        pattern = self.pattern
        with self._relay._lock:
            if pattern in self._relay._subs:
                try:
                    self._relay._subs[pattern].remove(entry)
                except ValueError:
                    # Another path already removed us (defensive; should be rare)
                    pass
                if not self._relay._subs[pattern]:
                    del self._relay._subs[pattern]
        self._entry = None

    async def __aenter__(self) -> Self:
        self._register()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        self._unregister()
        return False

    async def __aiter__(self) -> AsyncIterator[Msg[T]]:
        if self._entry is None:
            # ``async for relay.subscribe(...)`` without ``async with``: one enter/exit around the whole loop
            async with self:
                while True:
                    yield await self._queue.get()
        else:
            # ``async with`` already registered this queue; do not nest ``__aenter__`` again
            while True:
                yield await self._queue.get()


class Relay[T = Any]:
    """
    In-process pub/sub: dotted subjects, ``*`` wildcards, sync ``publish`` safe from any thread (``threading.Lock``).

    Each subscription binds to its loop. ``publish`` uses ``put_nowait`` when called on that loop's thread; otherwise
    it uses ``call_soon_threadsafe`` so other threads can enqueue safely.
    """

    __slots__ = ("_lock", "_subs")

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: dict[str, list[_Sub[T]]] = {}

    def publish(self, subject: str, data: T) -> None:
        """Copy subscriber list under lock; enqueue on the loop (direct or ``call_soon_threadsafe``) outside it."""
        msg: Msg[T] = (subject, data)

        # Match both exact segments and ``*`` wildcards, then snapshot who to notify
        with self._lock:
            to_notify: list[_Sub[T]] = []
            for pattern in _matching_patterns(subject):
                if subs := self._subs.get(pattern):
                    to_notify.extend(subs)

        # If this thread is inside an asyncio task, remember which loop is running here (small fixed cost per publish).
        # Worker threads, synchronous code, or "no loop in this thread" → RuntimeError → running stays None and
        # every subscriber is notified via call_soon_threadsafe below (no loop object needed in the publisher).
        try:
            running = get_running_loop()
        except RuntimeError as exc:
            if not _no_running_event_loop(exc):
                raise
            running = None

        # Never call subscriber code while holding the lock
        for queue, loop in to_notify:
            try:
                # asyncio.Queue is only safe to touch directly on its loop's thread; from anywhere else we must
                # schedule put_nowait on that loop (call_soon_threadsafe). Same thread + same loop → skip scheduling.
                if running is loop:
                    queue.put_nowait(msg)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, msg)
            except RuntimeError as exc:
                if not _closed_loop_enqueue_error(exc):
                    raise

    def subscribe(self, pattern: str) -> RelaySubscription[T]:
        """
        Return a subscription handle: register with ``async with``, then ``async for`` messages,
        or ``async for`` directly (registers on first iteration, unsubscribes when the loop ends).
        """
        return RelaySubscription(self, pattern)
