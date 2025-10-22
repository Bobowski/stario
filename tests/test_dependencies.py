from collections import defaultdict
from typing import Annotated, Awaitable

from starlette.testclient import TestClient

from stario import Stario
from stario.routes import Command


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


def test_dependency_override_basic():
    """Test basic dependency override for testing purposes."""
    call_counts = defaultdict(int)

    def real_dep() -> str:
        call_counts["real"] += 1
        return "real_value"

    def mock_dep() -> str:
        call_counts["mock"] += 1
        return "mock_value"

    def handler(value: Annotated[str, real_dep]) -> str:
        return value

    app = Stario(Command("/override", handler))

    # Without override
    with TestClient(app) as client:
        resp = client.post("/override")
    assert resp.text == "real_value"
    assert call_counts["real"] == 1
    assert call_counts["mock"] == 0

    # Reset for next test
    call_counts.clear()

    # With override using context manager
    with app.mocks({real_dep: mock_dep}):
        with TestClient(app) as client:
            resp = client.post("/override")

        assert resp.text == "mock_value"
        assert call_counts["real"] == 0
        assert call_counts["mock"] == 1

    # After context, should be back to real
    call_counts.clear()
    with TestClient(app) as client:
        resp = client.post("/override")
    assert resp.text == "real_value"
    assert call_counts["real"] == 1


def test_dependency_override_with_subdependencies():
    """Test that overrides work correctly with nested dependencies."""
    call_counts = defaultdict(int)

    def base_dep() -> int:
        call_counts["base"] += 1
        return 10

    def derived_dep(base: Annotated[int, base_dep]) -> int:
        call_counts["derived"] += 1
        return base + 5

    def mock_base() -> int:
        call_counts["mock_base"] += 1
        return 100

    def handler(value: Annotated[int, derived_dep]) -> str:
        return str(value)

    app = Stario(Command("/override-nested", handler))

    # With override on the base dependency
    with app.mocks({base_dep: mock_base}):
        with TestClient(app) as client:
            resp = client.post("/override-nested")

        assert resp.text == "105"  # 100 + 5
        assert call_counts["mock_base"] == 1
        assert call_counts["base"] == 0
        assert call_counts["derived"] == 1


def test_dependency_override_with_annotated_key():
    """Test that Annotated keys are properly unpacked."""
    call_counts = defaultdict(int)

    def real_dep() -> str:
        call_counts["real"] += 1
        return "real_value"

    def mock_dep() -> str:
        call_counts["mock"] += 1
        return "mock_value"

    def handler(value: Annotated[str, real_dep]) -> str:
        return value

    app = Stario(Command("/override", handler))

    # Using Annotated as key should be unpacked to the actual dependency
    with app.mocks({Annotated[str, real_dep]: mock_dep}):
        with TestClient(app) as client:
            resp = client.post("/override")

        assert resp.text == "mock_value"
        assert call_counts["mock"] == 1
        assert call_counts["real"] == 0
