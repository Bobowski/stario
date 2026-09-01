"""Tests for STARIO_LOOP resolution and third-party asyncio loop adapters."""

import asyncio
import sys
import types
from collections.abc import Coroutine
from typing import Any

import pytest

from stario.exceptions import StarioError
from stario.http.server import resolve_loop_runner


def test_resolve_loop_runner_asyncio() -> None:
    assert resolve_loop_runner("asyncio") is asyncio.run


def test_resolve_loop_runner_uses_module_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(coro: Coroutine[Any, Any, Any]) -> Any:
        return coro

    monkeypatch.setattr(
        "stario.http.server.importlib.import_module",
        lambda name: types.SimpleNamespace(run=fake_run),
    )
    assert resolve_loop_runner("zuvloop") is fake_run


def test_resolve_loop_runner_falls_back_to_new_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def factory() -> asyncio.AbstractEventLoop:
        return asyncio.new_event_loop()

    monkeypatch.setattr(
        "stario.http.server.importlib.import_module",
        lambda name: types.SimpleNamespace(new_event_loop=factory),
    )
    seen: dict[str, Any] = {}

    def fake_asyncio_run(
        coro: Coroutine[Any, Any, Any],
        *,
        loop_factory: Any = None,
        **kwargs: Any,
    ) -> str:
        seen["factory"] = loop_factory
        return "ok"

    monkeypatch.setattr("stario.http.server.asyncio.run", fake_asyncio_run)
    run = resolve_loop_runner("rloop")
    assert run(None) == "ok"  # type: ignore[arg-type]
    assert seen["factory"] is factory


def test_resolve_loop_runner_falls_back_to_event_loop_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Policy:
        def new_event_loop(self) -> asyncio.AbstractEventLoop:
            return asyncio.new_event_loop()

    monkeypatch.setattr(
        "stario.http.server.importlib.import_module",
        lambda name: types.SimpleNamespace(EventLoopPolicy=Policy),
    )
    seen: dict[str, Any] = {}

    def fake_asyncio_run(
        coro: Coroutine[Any, Any, Any],
        *,
        loop_factory: Any = None,
        **kwargs: Any,
    ) -> asyncio.AbstractEventLoop:
        seen["factory"] = loop_factory
        return loop_factory()

    monkeypatch.setattr("stario.http.server.asyncio.run", fake_asyncio_run)
    run = resolve_loop_runner("uringcore")
    loop = run(None)  # type: ignore[arg-type]
    assert isinstance(loop, asyncio.AbstractEventLoop)
    loop.close()
    assert callable(seen["factory"])


def test_resolve_loop_runner_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_import(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr("stario.http.server.importlib.import_module", fake_import)
    with pytest.raises(StarioError, match="uvloop is not installed"):
        resolve_loop_runner("uvloop")


def test_resolve_loop_runner_missing_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "stario.http.server.importlib.import_module",
        lambda name: types.SimpleNamespace(),
    )
    with pytest.raises(StarioError, match="does not expose"):
        resolve_loop_runner("rloop")


def test_resolve_loop_runner_rejects_uvloop_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stario.http.server.sys.platform", "win32")
    with pytest.raises(StarioError, match="uvloop is not supported on Windows"):
        resolve_loop_runner("uvloop")


@pytest.mark.skipif(sys.platform == "win32", reason="winloop is the Windows extra")
def test_resolve_loop_runner_rejects_winloop_on_posix() -> None:
    with pytest.raises(StarioError, match="winloop is only supported on Windows"):
        resolve_loop_runner("winloop")


def test_resolve_loop_runner_rejects_uringcore_off_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stario.http.server.sys.platform", "darwin")
    with pytest.raises(StarioError, match="uringcore is only supported on Linux"):
        resolve_loop_runner("uringcore")
