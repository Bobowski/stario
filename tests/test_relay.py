"""Tests for stario.relay - In-process pub/sub."""

import asyncio

import pytest

from stario.relay import Relay, _deduplicate_patterns, _matching_patterns


def _pattern_cleared(relay: Relay, pattern: str) -> bool:
    """True when no subscribers remain at this pattern string."""
    return pattern not in relay._subs


def _naive_pattern_enumeration(subject: str) -> tuple[str, ...]:
    """Reference list of pattern strings (tests only; not used by ``Relay.publish``)."""
    parts = subject.split(".")
    patterns = [subject]
    for i in range(len(parts) - 1, 0, -1):
        patterns.append(".".join(parts[:i]) + ".*")
    patterns.append("*")
    return tuple(patterns)


class TestMatchingPatterns:
    """Naive pattern enumeration stays aligned with relay semantics (documentation / regression)."""

    def test_simple_subject(self):
        patterns = _naive_pattern_enumeration("room.123")
        assert "room.123" in patterns
        assert "room.*" in patterns
        assert "*" in patterns

    def test_deep_subject(self):
        patterns = _naive_pattern_enumeration("room.123.moves.1")
        assert "room.123.moves.1" in patterns
        assert "room.123.moves.*" in patterns
        assert "room.123.*" in patterns
        assert "room.*" in patterns
        assert "*" in patterns

    def test_single_segment(self):
        patterns = _naive_pattern_enumeration("simple")
        assert "simple" in patterns
        assert "*" in patterns

    def test_order_most_specific_first(self):
        patterns = _naive_pattern_enumeration("a.b.c")
        # Exact match should be first
        assert patterns[0] == "a.b.c"
        # Wildcard should be last
        assert patterns[-1] == "*"

    def test_matching_patterns_same_as_naive_as_set(self):
        """``_matching_patterns`` is the same key set as the naive tuple (order not defined)."""
        subject = "room.123.moves"
        assert _matching_patterns(subject) == frozenset(_naive_pattern_enumeration(subject))

    def test_matching_patterns_literal_star_segment_no_duplicate_row(self):
        """Last segment ``*``: loop must not repeat the full subject before ``frozenset``."""
        assert _matching_patterns("room.123.*") == frozenset({"room.123.*", "room.*", "*"})


class TestDeduplicatePatterns:
    def test_star_subsumes_all(self):
        assert _deduplicate_patterns(("users.*", "*")) == frozenset({"*"})

    def test_users_wildcard_subsumes_exact_user_id(self):
        assert _deduplicate_patterns(("users.*", "users.user1234")) == frozenset({"users.*"})

    def test_independent_exact_patterns_kept(self):
        assert _deduplicate_patterns(("users.a", "users.b")) == frozenset({"users.a", "users.b"})

    def test_order_independent_same_result(self):
        a = _deduplicate_patterns(("users.a", "users.b", "users.*"))
        b = _deduplicate_patterns(("users.*", "users.b", "users.a"))
        assert a == b == frozenset({"users.*"})

    def test_wildcard_prefix_chain_keeps_broadest(self):
        assert _deduplicate_patterns(("room.123.moves.*", "room.*", "room.123.*")) == frozenset({"room.*"})

    def test_idempotent(self):
        once = _deduplicate_patterns(("a.*", "a.b", "a.b.c"))
        assert _deduplicate_patterns(tuple(once)) == once

    def test_invariant_redundant_patterns_skipped(self):
        """Every dropped pattern has some kept key in its ``_matching_patterns`` chain."""
        raw = ("users.*", "users.x", "room.123.*", "room.*", "*")
        out = set(_deduplicate_patterns(raw))
        assert out == {"*"}
        for q in raw:
            if q in out:
                continue
            chain = set(_matching_patterns(q))
            assert chain & out


class TestRelayBasic:
    """Test basic Relay functionality."""

    def test_subscribe_requires_at_least_one_pattern(self):
        relay = Relay()
        with pytest.raises(TypeError, match="at least one pattern"):
            relay.subscribe()  # type: ignore[call-arg]

    def test_create_relay(self):
        relay = Relay()
        assert relay is not None

    async def test_publish_no_subscribers(self):
        relay = Relay()
        # Should not raise even with no subscribers
        relay.publish("test.subject", {"data": 1})

    async def test_subscribe_receive(self):
        relay = Relay()
        received = []

        async def subscriber():
            async for subject, data in relay.subscribe("test.*"):
                received.append((subject, data))
                if len(received) >= 2:
                    break

        # Start subscriber task
        task = asyncio.create_task(subscriber())

        # Give subscriber time to start
        await asyncio.sleep(0.01)

        # Publish messages
        relay.publish("test.one", {"msg": 1})
        relay.publish("test.two", {"msg": 2})

        # Wait for subscriber to receive
        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 2
        assert received[0] == ("test.one", {"msg": 1})
        assert received[1] == ("test.two", {"msg": 2})


class TestRelayPatterns:
    """Test pattern matching."""

    async def test_exact_match(self):
        relay = Relay()
        received = []

        async def subscriber():
            async for subject, data in relay.subscribe("exact.match"):
                received.append(subject)
                break

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        relay.publish("exact.match", None)
        relay.publish("exact.other", None)  # Should not match

        await asyncio.wait_for(task, timeout=1.0)

        assert received == ["exact.match"]

    async def test_catchall_pattern(self):
        relay = Relay()
        received = []

        async def subscriber():
            async for subject, data in relay.subscribe("room.123.*"):
                received.append(subject)
                if len(received) >= 3:
                    break

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        relay.publish("room.123.moves", None)
        relay.publish("room.123.chat", None)
        relay.publish("room.123.leave", None)
        relay.publish("room.456.moves", None)  # Different room, should not match

        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 3
        assert "room.123.moves" in received
        assert "room.123.chat" in received
        assert "room.123.leave" in received

    async def test_global_wildcard(self):
        relay = Relay()
        received = []

        async def subscriber():
            async for subject, data in relay.subscribe("*"):
                received.append(subject)
                if len(received) >= 2:
                    break

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        relay.publish("anything.here", None)
        relay.publish("something.else", None)

        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 2


class TestRelayCleanup:
    """Test subscription cleanup."""

    async def test_unsubscribe_on_exit(self):
        relay = Relay()
        cleanup_done = asyncio.Event()

        async def subscriber():
            try:
                async for _ in relay.subscribe("cleanup.test"):
                    break
            finally:
                # Give generator time to cleanup
                await asyncio.sleep(0)
                cleanup_done.set()

        # Start subscriber and publish a message so it can exit
        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)
        relay.publish("cleanup.test", None)
        await asyncio.wait_for(cleanup_done.wait(), timeout=1.0)
        await task

        # After subscriber exits, pattern should be removed
        assert _pattern_cleared(relay, "cleanup.test")


class TestRelayMultipleSubscribers:
    """Test multiple subscribers."""

    async def test_multiple_subscribers_same_pattern(self):
        relay = Relay()
        received1 = []
        received2 = []

        async def sub1():
            async for subject, data in relay.subscribe("shared.*"):
                received1.append(subject)
                break

        async def sub2():
            async for subject, data in relay.subscribe("shared.*"):
                received2.append(subject)
                break

        task1 = asyncio.create_task(sub1())
        task2 = asyncio.create_task(sub2())
        await asyncio.sleep(0.01)

        relay.publish("shared.message", None)

        await asyncio.wait_for(asyncio.gather(task1, task2), timeout=1.0)

        # Both should receive the message
        assert received1 == ["shared.message"]
        assert received2 == ["shared.message"]


class TestRelayRaceConditions:
    """Test race condition handling in Relay."""

    async def test_cleanup_after_subscriber_exits(self):
        """Test that pattern is cleaned up after subscriber exits."""
        relay = Relay()
        cleanup_complete = asyncio.Event()

        # Create a subscriber that exits after first message
        async def subscriber():
            try:
                async for subject, data in relay.subscribe("race.test"):
                    break
            finally:
                # Need to yield to allow generator cleanup
                await asyncio.sleep(0)
                cleanup_complete.set()

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        # Publish to unblock
        relay.publish("race.test", None)
        await asyncio.wait_for(task, timeout=1.0)
        await asyncio.wait_for(cleanup_complete.wait(), timeout=1.0)

        # Pattern should be cleaned up
        assert _pattern_cleared(relay, "race.test")

    async def test_concurrent_unsubscribe(self):
        """Test that concurrent unsubscribes don't cause issues."""
        relay = Relay()
        unsubscribed = [0]
        cleanup_events = [asyncio.Event() for _ in range(5)]

        async def quick_subscriber(idx: int):
            try:
                async for _ in relay.subscribe("concurrent.*"):
                    break
            finally:
                await asyncio.sleep(0)
                unsubscribed[0] += 1
                cleanup_events[idx].set()

        # Start multiple subscribers
        tasks = [asyncio.create_task(quick_subscriber(i)) for i in range(5)]
        await asyncio.sleep(0.01)

        # Publish to unblock all
        relay.publish("concurrent.msg", None)

        # All should complete without error
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
        await asyncio.wait_for(
            asyncio.gather(*[e.wait() for e in cleanup_events]), timeout=1.0
        )

        assert unsubscribed[0] == 5
        assert _pattern_cleared(relay, "concurrent.*")


class TestRelaySubscribeMultiplePatterns:
    async def test_one_delivery_when_patterns_overlap(self):
        relay = Relay()
        received: list[tuple[str, int]] = []

        async def subscriber():
            async for subject, data in relay.subscribe("*", "users.*"):
                received.append((subject, data))
                if len(received) >= 2:
                    break

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)
        relay.publish("users.a.b", 1)
        relay.publish("other.x", 2)
        await asyncio.wait_for(task, timeout=1.0)
        assert received == [("users.a.b", 1), ("other.x", 2)]

    async def test_unsubscribe_removes_all_patterns(self):
        relay = Relay()

        async def subscriber():
            async with relay.subscribe("chat.*", "room.*") as sub:
                relay.publish("chat.msg", None)
                async for _ in sub:
                    break

        task = asyncio.create_task(subscriber())
        await asyncio.wait_for(task, timeout=1.0)
        assert _pattern_cleared(relay, "chat.*")
        assert _pattern_cleared(relay, "room.*")


class TestRelaySubscriptionOrdering:
    """Subscribe is active before the first ``queue.get()`` — safe to publish after ``async with``."""

    async def test_events_after_enter_not_missed(self):
        relay = Relay[str]()
        received: list[str] = []

        async def subscriber():
            async with relay.subscribe("room.*") as sub:
                relay.publish("room.a", "during-setup")
                async for subject, data in sub:
                    received.append(data)
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)
        relay.publish("room.b", "after-loop-start")
        await asyncio.wait_for(task, timeout=1.0)

        assert received == ["during-setup", "after-loop-start"]
