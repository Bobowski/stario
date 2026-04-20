"""
Process-local pub/sub: dotted subjects, ``*`` segment wildcards.

``publish`` is synchronous and thread-safe. Subscriptions are bound to the asyncio loop that created them.
This is not a message broker—no cross-process delivery, durability, or global ordering guarantees.
"""

from asyncio import AbstractEventLoop, Queue, get_running_loop
from collections.abc import AsyncIterator
from functools import lru_cache
from threading import Lock
from types import TracebackType
from typing import Any, Self

# Message: (subject, data)
type Msg[T = Any] = tuple[str, T]

# (queue, loop) — put on ``loop`` via ``call_soon_threadsafe`` when not on that loop's thread
type _Sub[T] = tuple[Queue[Msg[T]], AbstractEventLoop]


def _no_running_event_loop(exc: RuntimeError) -> bool:
    """No running asyncio loop in this thread."""
    return "no running event loop" in str(exc).lower()


def _closed_loop_enqueue_error(exc: RuntimeError) -> bool:
    """Enqueue failed: target loop is closed."""
    msg = str(exc).lower()
    if "event loop is closed" in msg:
        return True
    if "cannot schedule" in msg and "closed" in msg:
        return True
    return False


@lru_cache(maxsize=1024)
def _matching_patterns(subject: str) -> frozenset[str]:
    """Lookup keys for ``subject`` as a published topic: ``{subject, *}`` plus each ``prefix.*`` (unordered)."""
    parts = subject.split(".")
    out: set[str] = {subject, "*"}
    for i in range(len(parts) - 1, 0, -1):
        out.add(".".join(parts[:i]) + ".*")
    return frozenset(out)


@lru_cache(maxsize=1024)
def _deduplicate_patterns(patterns: tuple[str, ...]) -> frozenset[str]:
    """Minimal pattern set: drop entries subsumed by another via ``_matching_patterns`` (visit ``(len, lex)``)."""
    uniq = tuple(dict.fromkeys(patterns))
    if len(uniq) <= 1:
        return frozenset(uniq)

    kept: set[str] = set()
    for q in sorted(uniq, key=lambda p: (len(p), p)):
        chain = _matching_patterns(q)
        if any(p in kept for p in chain):
            continue
        kept.add(q)
    return frozenset(kept)


class _RelaySubscription[T = Any]:
    """Internal: ``subscribe`` handle (async context manager + async iterable)."""

    __slots__ = ("_relay", "patterns", "_queue", "_entry")

    def __init__(self, relay: Relay[T], patterns: frozenset[str]) -> None:
        self._relay = relay
        self.patterns = patterns
        self._queue = Queue()
        self._entry: _Sub[T] | None = None

    @property
    def pattern(self) -> str:
        """Arbitrary representative when multiple patterns exist (``min`` string)."""
        return min(self.patterns)

    def _register(self) -> None:
        if self._entry is not None:
            raise RuntimeError("subscription is already active")
        loop = get_running_loop()
        entry: _Sub[T] = (self._queue, loop)
        self._entry = entry
        with self._relay._lock:
            for pat in self.patterns:
                if pat not in self._relay._subs:
                    self._relay._subs[pat] = []
                self._relay._subs[pat].append(entry)

    def _unregister(self) -> None:
        entry = self._entry
        if entry is None:
            return
        with self._relay._lock:
            for pattern in self.patterns:
                if pattern in self._relay._subs:
                    try:
                        self._relay._subs[pattern].remove(entry)
                    except ValueError:
                        # Defensive: removal already happened on another path.
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
            # ``async for relay.subscribe(...)`` without ``async with``: one enter/exit around the whole loop.
            async with self:
                while True:
                    yield await self._queue.get()
        else:
            # ``async with`` already registered; do not nest ``__aenter__``.
            while True:
                yield await self._queue.get()


class Relay[T = Any]:
    """
    In-process publish/subscribe registry.

    **Subjects** are dot-separated strings. A subscription key like ``a.b.*`` or ``a.*`` matches published subjects
    for which that key appears on the match ladder (exact topic, then shorter ``prefix.*``, then ``*``)—so broader
    prefixes receive deeper subjects. A lone ``*`` receives all messages.

    **Threading:** ``publish`` may be called from any thread; it only holds the lock while copying subscriber
    lists, then enqueues per subscriber loop (``put_nowait`` or ``call_soon_threadsafe``). No subscriber body runs
    under the lock.

    **Async:** Each subscription is tied to the event loop that registered it (``async with`` / first ``async for``).
    """

    __slots__ = ("_lock", "_subs")

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: dict[str, list[_Sub[T]]] = {}

    def publish(self, subject: str, data: T) -> None:
        """
        Deliver ``(subject, data)`` to every subscriber whose pattern matches ``subject``.

        Thread-safe: resolves matching pattern keys under a lock, then enqueues without holding the lock.
        Does not await subscribers; failures to enqueue (e.g. closed loop) are swallowed where safe.
        """
        msg: Msg[T] = (subject, data)

        with self._lock:
            to_notify: list[_Sub[T]] = []
            for pattern in _matching_patterns(subject):
                if subs := self._subs.get(pattern):
                    to_notify.extend(subs)

        try:
            running = get_running_loop()
        except RuntimeError as exc:
            if not _no_running_event_loop(exc):
                raise
            running = None

        for queue, loop in to_notify:
            try:
                if running is loop:
                    queue.put_nowait(msg)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, msg)
            except RuntimeError as exc:
                if not _closed_loop_enqueue_error(exc):
                    raise

    def subscribe(self, *patterns: str) -> _RelaySubscription[T]:
        """
        Subscribe to one or more patterns. Overlapping patterns are reduced to a minimal equivalent set.

        Use ``async with relay.subscribe(...) as sub:`` and ``async for msg in sub:``, or ``async for`` directly
        on ``subscribe(...)`` (registers for the lifetime of the loop). Requires at least one pattern.
        """
        if not patterns:
            raise TypeError("subscribe() requires at least one pattern")
        return _RelaySubscription(self, _deduplicate_patterns(tuple(patterns)))
