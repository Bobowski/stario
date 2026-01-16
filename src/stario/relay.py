"""
Relay - Lightweight In-Process Pub/Sub.

NATS-inspired subject-based messaging for CQRS patterns.
Fire-and-forget publishing, async subscription with auto-cleanup.

Subject Patterns:
    room.123        - Exact match only
    room.123.*      - Catch-all: anything starting with "room.123."
    room.*          - Catch-all: anything starting with "room."
    *               - Match everything

Usage:
    relay = Relay()

    # Publish (sync, fire-and-forget)
    relay.publish("room.123.moves", None)
    relay.publish("room.123.moves", move_data)

    # Subscribe (async iterator, auto-cleanup)
    async for subject, data in relay.subscribe("room.123.*"):
        print(subject, data)
"""

from asyncio import AbstractEventLoop, Queue, get_running_loop
from collections.abc import AsyncIterator
from functools import lru_cache
from threading import Lock
from typing import Any

# Message: (subject, data)
type Msg[T = Any] = tuple[str, T]

# Subscriber: (queue, loop)
type _Sub[T] = tuple[Queue[Msg[T]], AbstractEventLoop]


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


class Relay[T = Any]:
    """
    In-process pub/sub. Thread-safe, minimal locking.

    Threading model:
    - Lock is held only for dict/list operations (microseconds).
    - Queue delivery happens outside the lock via loop.call_soon_threadsafe().
    - This allows publish() to be called from any thread (e.g., background workers).

    Why threading.Lock instead of asyncio.Lock?
    - publish() must be callable from sync code (fire-and-forget pattern).
    - asyncio.Lock requires await, which would force publish() to be async.
    - The lock hold time is negligible (just dict operations), so no contention.

    Memory management:
    - Subscribers are cleaned up automatically when the async iterator exits.
    - Empty pattern lists are removed to prevent memory leaks from unused patterns.
    """

    __slots__ = ("_lock", "_subs")

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: dict[str, list[_Sub[T]]] = {}

    def publish(self, subject: str, data: T) -> None:
        """
        Publish to a subject. Fire-and-forget.

        Thread-safe. Lock held only to collect subscribers.
        """
        msg: Msg[T] = (subject, data)

        # Short lock - just collect subscribers
        with self._lock:
            to_notify: list[_Sub[T]] = []
            for pattern in _matching_patterns(subject):
                if subs := self._subs.get(pattern):
                    to_notify.extend(subs)

        # Deliver outside lock
        for queue, loop in to_notify:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, msg)
            except RuntimeError:
                pass  # Loop closed

    async def subscribe(self, pattern: str) -> AsyncIterator[Msg[T]]:
        """
        Subscribe to a pattern. Auto-cleanup on exit.

        Patterns:
            "room.123"   - Exact match
            "room.123.*" - Catch-all
            "*"          - Everything
        """
        queue: Queue[Msg[T]] = Queue()
        loop = get_running_loop()
        entry: _Sub[T] = (queue, loop)

        with self._lock:
            if pattern not in self._subs:
                self._subs[pattern] = []
            self._subs[pattern].append(entry)

        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                # Safe removal - handle race conditions where entry may already be gone
                if pattern in self._subs:
                    try:
                        self._subs[pattern].remove(entry)
                    except ValueError:
                        pass  # Already removed (race condition)
                    if not self._subs[pattern]:
                        del self._subs[pattern]
