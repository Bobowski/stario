from collections import defaultdict
from typing import Annotated, Awaitable

from starlette.testclient import TestClient

from stario import Command, Stario


def test_dependencies_simple_ok():

    def dep1() -> int:
        return 1

    def dep2() -> int:
        return 2

    def dep3(d1: Annotated[int, dep1]) -> int:
        return d1 + 3

    def handler(
        d1: Annotated[int, dep1], d2: Annotated[int, dep2], d3: Annotated[int, dep3]
    ) -> str:
        return str(d1 + d2 + d3)

    app = Stario(Command("/dep", handler))

    with TestClient(app) as client:
        resp = client.post("/dep")

    assert resp.text == "7"  # 1 + 2 + (1 + 3)
    assert resp.status_code == 200


def test_dependencies_lifetimes_request():

    call_counts = defaultdict(int)

    def dep1() -> int:
        # Count how many times this is called
        call_counts["dep1"] += 1
        return 1

    def dep2(d1: Annotated[int, dep1, "request"]) -> int:
        call_counts["dep2"] += 1
        return 2

    def dep3(
        d1: Annotated[int, dep1, "request"],
        d2: Annotated[int, dep2, "request"],
    ) -> int:
        call_counts["dep3"] += 1
        return d1 + d2 + 3

    def handler(
        d1: Annotated[int, dep1, "request"],
        d2: Annotated[int, dep2, "request"],
        d3: Annotated[int, dep3, "request"],
    ) -> str:
        return str(d1 + d2 + d3)

    app = Stario(Command("/dep", handler))

    with TestClient(app) as client:
        resp = client.post("/dep")

    assert resp.text == "9"  # 1 + 2 + (1 + 2 + 3)
    assert resp.status_code == 200

    # All should be called only once - request level scope
    assert call_counts["dep1"] == 1
    assert call_counts["dep2"] == 1
    assert call_counts["dep3"] == 1


def test_dependencies_lifetimes_transient():

    call_counts = defaultdict(int)

    def dep1() -> int:
        # Count how many times this is called
        call_counts["dep1"] += 1
        return 1

    def dep2(d1: Annotated[int, dep1, "transient"]) -> int:
        call_counts["dep2"] += 1
        return 2

    def dep3(
        d1: Annotated[int, dep1, "transient"],
        d2: Annotated[int, dep2, "transient"],
    ) -> int:
        call_counts["dep3"] += 1
        return d1 + d2 + 3

    def handler(
        d1: Annotated[int, dep1, "transient"],
        d2: Annotated[int, dep2, "transient"],
        d3: Annotated[int, dep3, "transient"],
    ) -> str:
        return str(d1 + d2 + d3)

    app = Stario(Command("/dep", handler))

    with TestClient(app) as client:
        resp = client.post("/dep")

    assert resp.text == "9"  # 1 + 2 + (1 + 2 + 3)
    assert resp.status_code == 200

    # All should be called only once - transient level scope
    assert call_counts["dep1"] == 4
    assert call_counts["dep2"] == 2
    assert call_counts["dep3"] == 1


def test_dependencies_lifetimes_singleton():

    call_counts = defaultdict(int)

    def dep1() -> int:
        # Count how many times this is called
        call_counts["dep1"] += 1
        return 1

    def dep2(d1: Annotated[int, dep1, "singleton"]) -> int:
        call_counts["dep2"] += 1
        return 2

    def dep3() -> int:
        call_counts["dep3"] += 1
        return 3

    def handler(
        d1: Annotated[int, dep1, "singleton"],
        d2: Annotated[int, dep2, "singleton"],
        d3: Annotated[int, dep3, "request"],
    ) -> str:
        return str(d1 + d2)

    app = Stario(Command("/dep", handler))

    with TestClient(app) as client:
        resp1 = client.post("/dep")
        resp2 = client.post("/dep")

    assert resp1.text == "3"  # 1 + 2
    assert resp1.status_code == 200
    assert resp2.text == "3"  # 1 + 2
    assert resp2.status_code == 200

    assert call_counts["dep1"] == 1  # only on first request
    assert call_counts["dep2"] == 1
    assert call_counts["dep3"] == 2  # both requests


def test_dependencies_lazy_awaitable():

    call_counts = defaultdict(int)

    def slow_dep() -> int:
        # Count how many times this is called
        call_counts["slow_dep"] += 1
        return 42

    async def handler(
        lazy_value: Annotated[Awaitable[int], slow_dep, "lazy"],
    ) -> str:
        # At this point, slow_dep has NOT been called yet
        assert call_counts["slow_dep"] == 0
        # Only when we await the lazy_value should it be called
        result = await lazy_value
        assert result == 42
        assert call_counts["slow_dep"] == 1
        return f"Lazy resolved to: {result}"

    app = Stario(Command("/dep", handler))

    with TestClient(app) as client:
        resp = client.post("/dep")

    assert resp.status_code == 200
    assert resp.text == "Lazy resolved to: 42"
    # Verify the dependency was only called once
    assert call_counts["slow_dep"] == 1


def test_dependencies_lazy_with_subdependencies():

    call_counts = defaultdict(int)

    def base_dep() -> int:
        call_counts["base_dep"] += 1
        return 10

    def derived_dep(base: Annotated[int, base_dep]) -> int:
        call_counts["derived_dep"] += 1
        return base + 5

    async def handler(
        lazy_value: Annotated[Awaitable[int], derived_dep, "lazy"],
    ) -> str:
        # Neither dependency has been called yet
        assert call_counts["base_dep"] == 0
        assert call_counts["derived_dep"] == 0

        # Await the lazy dependency - this should resolve both base_dep and derived_dep
        result = await lazy_value
        assert result == 15  # 10 + 5
        assert call_counts["base_dep"] == 1
        assert call_counts["derived_dep"] == 1
        return f"Lazy resolved to: {result}"

    app = Stario(Command("/dep", handler))

    with TestClient(app) as client:
        resp = client.post("/dep")

    assert resp.status_code == 200
    assert resp.text == "Lazy resolved to: 15"
    # Both dependencies called once
    assert call_counts["base_dep"] == 1
    assert call_counts["derived_dep"] == 1


def test_dependencies_lazy_with_async_function():

    call_counts = defaultdict(int)

    async def async_dep() -> int:
        call_counts["async_dep"] += 1
        # Simulate some async work
        return 99

    async def handler(
        lazy_value: Annotated[Awaitable[int], async_dep, "lazy"],
    ) -> str:
        # The async function has NOT been called yet
        assert call_counts["async_dep"] == 0

        # Await the lazy dependency
        result = await lazy_value
        assert result == 99
        assert call_counts["async_dep"] == 1
        return f"Async lazy resolved to: {result}"

    app = Stario(Command("/dep", handler))

    with TestClient(app) as client:
        resp = client.post("/dep")

    assert resp.status_code == 200
    assert resp.text == "Async lazy resolved to: 99"
    assert call_counts["async_dep"] == 1


# ============================================================================
# Context Manager Tests
# ============================================================================


def test_context_manager_sync_basic():
    """Test basic synchronous context manager dependency."""
    events = []

    class SyncResource:
        def __enter__(self):
            events.append("enter")
            return "resource_value"

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append("exit")
            return False

    def get_resource() -> SyncResource:
        events.append("create")
        return SyncResource()

    def handler(resource: Annotated[str, get_resource]) -> str:
        events.append("handler")
        return f"Got: {resource}"

    app = Stario(Command("/sync-cm", handler))

    with TestClient(app) as client:
        resp = client.post("/sync-cm")

    assert resp.status_code == 200
    assert resp.text == "Got: resource_value"
    # Verify lifecycle: create -> enter -> handler -> exit
    assert events == ["create", "enter", "handler", "exit"]


def test_context_manager_async_basic():
    """Test basic asynchronous context manager dependency."""
    events = []

    class AsyncResource:
        async def __aenter__(self):
            events.append("aenter")
            return "async_resource_value"

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            events.append("aexit")
            return False

    async def get_async_resource() -> AsyncResource:
        events.append("create")
        return AsyncResource()

    async def handler(resource: Annotated[str, get_async_resource]) -> str:
        events.append("handler")
        return f"Got: {resource}"

    app = Stario(Command("/async-cm", handler))

    with TestClient(app) as client:
        resp = client.post("/async-cm")

    assert resp.status_code == 200
    assert resp.text == "Got: async_resource_value"
    # Verify lifecycle: create -> aenter -> handler -> aexit
    assert events == ["create", "aenter", "handler", "aexit"]


def test_context_manager_multiple_dependencies():
    """Test multiple context manager dependencies in a single handler."""
    events = []

    class Resource:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            events.append(f"enter_{self.name}")
            return self.name

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append(f"exit_{self.name}")
            return False

    def get_resource_a() -> Resource:
        events.append("create_a")
        return Resource("a")

    def get_resource_b() -> Resource:
        events.append("create_b")
        return Resource("b")

    def handler(
        res_a: Annotated[str, get_resource_a],
        res_b: Annotated[str, get_resource_b],
    ) -> str:
        events.append("handler")
        return f"Got: {res_a}, {res_b}"

    app = Stario(Command("/multi-cm", handler))

    with TestClient(app) as client:
        resp = client.post("/multi-cm")

    assert resp.status_code == 200
    assert resp.text == "Got: a, b"
    # Both resources should be created, entered, used, and exited
    assert "create_a" in events
    assert "create_b" in events
    assert "enter_a" in events
    assert "enter_b" in events
    assert "exit_a" in events
    assert "exit_b" in events
    # Verify handler was called
    assert "handler" in events


def test_context_manager_nested_dependencies():
    """Test context manager with nested dependency injection."""
    events = []

    class Database:
        def __enter__(self):
            events.append("db_enter")
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append("db_exit")
            return False

        def query(self):
            return "result"

    def get_db() -> Database:
        events.append("db_create")
        return Database()

    class Cache:
        def __init__(self, db: Database):
            self.db = db

        def __enter__(self):
            events.append("cache_enter")
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append("cache_exit")
            return False

        def get(self):
            return self.db.query()

    def get_cache(db: Annotated[Database, get_db]) -> Cache:
        events.append("cache_create")
        return Cache(db)

    def handler(cache: Annotated[Cache, get_cache]) -> str:
        events.append("handler")
        return f"Cache result: {cache.get()}"

    app = Stario(Command("/nested-cm", handler))

    with TestClient(app) as client:
        resp = client.post("/nested-cm")

    assert resp.status_code == 200
    assert resp.text == "Cache result: result"
    # Verify order: db created/entered before cache
    assert events.index("db_create") < events.index("cache_create")
    assert events.index("db_enter") < events.index("cache_enter")
    # Verify cleanup order: cache exited before db (LIFO)
    assert events.index("cache_exit") < events.index("db_exit")


def test_context_manager_exception_cleanup():
    """Test that context manager cleanup is called even on exception."""
    events = []

    class Resource:
        def __enter__(self):
            events.append("enter")
            return "value"

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append("exit")
            # Return False to let exception propagate
            return False

    def get_resource() -> Resource:
        events.append("create")
        return Resource()

    def handler(resource: Annotated[str, get_resource]) -> str:
        events.append("handler")
        raise ValueError("Something went wrong")

    app = Stario(Command("/cm-error", handler))

    with TestClient(app) as client:
        resp = client.post("/cm-error")

    # Should get 500 error due to unhandled exception
    assert resp.status_code == 500
    # But cleanup should still happen
    assert events == ["create", "enter", "handler", "exit"]


def test_context_manager_multiple_async():
    """Test multiple async context managers."""
    events = []

    class AsyncResource:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            events.append(f"aenter_{self.name}")
            return self.name

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            events.append(f"aexit_{self.name}")
            return False

    async def get_resource_1() -> AsyncResource:
        events.append("create_1")
        return AsyncResource("1")

    async def get_resource_2() -> AsyncResource:
        events.append("create_2")
        return AsyncResource("2")

    async def handler(
        res1: Annotated[str, get_resource_1],
        res2: Annotated[str, get_resource_2],
    ) -> str:
        events.append("handler")
        return f"Got: {res1}, {res2}"

    app = Stario(Command("/multi-async-cm", handler))

    with TestClient(app) as client:
        resp = client.post("/multi-async-cm")

    assert resp.status_code == 200
    assert resp.text == "Got: 1, 2"
    assert "create_1" in events
    assert "create_2" in events
    assert "aenter_1" in events
    assert "aenter_2" in events
    assert "aexit_1" in events
    assert "aexit_2" in events


def test_context_manager_mixed_sync_async():
    """Test mixing sync and async context managers in same handler."""
    events = []

    class SyncResource:
        def __enter__(self):
            events.append("sync_enter")
            return "sync"

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append("sync_exit")
            return False

    class AsyncResource:
        async def __aenter__(self):
            events.append("async_enter")
            return "async"

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            events.append("async_exit")
            return False

    def get_sync() -> SyncResource:
        events.append("sync_create")
        return SyncResource()

    async def get_async() -> AsyncResource:
        events.append("async_create")
        return AsyncResource()

    async def handler(
        sync_res: Annotated[str, get_sync],
        async_res: Annotated[str, get_async],
    ) -> str:
        events.append("handler")
        return f"{sync_res}_{async_res}"

    app = Stario(Command("/mixed-cm", handler))

    with TestClient(app) as client:
        resp = client.post("/mixed-cm")

    assert resp.status_code == 200
    assert resp.text == "sync_async"
    # Verify both enter and exit are called
    assert "sync_enter" in events
    assert "sync_exit" in events
    assert "async_enter" in events
    assert "async_exit" in events


def test_context_manager_with_request_lifetime():
    """Test that context managers work with request lifetime."""
    call_counts = defaultdict(int)
    events = []

    class Resource:
        def __enter__(self):
            events.append("enter")
            return "value"

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append("exit")
            return False

    def get_resource() -> Resource:
        call_counts["calls"] += 1
        events.append("create")
        return Resource()

    def handler(resource: Annotated[str, get_resource, "request"]) -> str:
        return f"Got: {resource}"

    app = Stario(Command("/cm-lifetime", handler))

    with TestClient(app) as client:
        resp1 = client.post("/cm-lifetime")
        resp2 = client.post("/cm-lifetime")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Each request should create its own resource
    assert call_counts["calls"] == 2
    # And each should be cleaned up
    assert events.count("enter") == 2
    assert events.count("exit") == 2


def test_context_manager_singleton_lifetime():
    """Test that context managers work with singleton lifetime."""
    call_counts = defaultdict(int)
    events = []

    class Singleton:
        def __enter__(self):
            events.append("enter")
            return "value"

        def __exit__(self, exc_type, exc_val, exc_tb):
            events.append("exit")
            return False

    def get_singleton() -> Singleton:
        call_counts["calls"] += 1
        events.append("create")
        return Singleton()

    def handler(resource: Annotated[str, get_singleton, "singleton"]) -> str:
        return f"Got: {resource}"

    app = Stario(Command("/cm-singleton", handler))

    with TestClient(app) as client:
        resp1 = client.post("/cm-singleton")
        resp2 = client.post("/cm-singleton")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Singleton should only be created once
    assert call_counts["calls"] == 1
    # But we should still see enter/exit for each request?
    # Actually, for singleton the context manager enters/exits only once during creation
    assert events.count("enter") == 1
    assert events.count("exit") == 1
