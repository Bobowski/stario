"""Tests for stario.relay - In-process pub/sub."""

import asyncio

from stario.relay import Relay, _matching_patterns


class TestMatchingPatterns:
    """Test pattern generation for subject matching."""

    def test_simple_subject(self):
        patterns = _matching_patterns("room.123")
        assert "room.123" in patterns
        assert "room.*" in patterns
        assert "*" in patterns

    def test_deep_subject(self):
        patterns = _matching_patterns("room.123.moves.1")
        assert "room.123.moves.1" in patterns
        assert "room.123.moves.*" in patterns
        assert "room.123.*" in patterns
        assert "room.*" in patterns
        assert "*" in patterns

    def test_single_segment(self):
        patterns = _matching_patterns("simple")
        assert "simple" in patterns
        assert "*" in patterns

    def test_order_most_specific_first(self):
        patterns = _matching_patterns("a.b.c")
        # Exact match should be first
        assert patterns[0] == "a.b.c"
        # Wildcard should be last
        assert patterns[-1] == "*"


class TestRelayBasic:
    """Test basic Relay functionality."""

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
        assert "cleanup.test" not in relay._subs


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
        assert "race.test" not in relay._subs

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
        assert "concurrent.*" not in relay._subs
