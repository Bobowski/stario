"""Unit tests for stario._env helpers."""

import pytest

from stario._env import (
    env_bool,
    env_int,
    env_octal_mode,
    env_path,
    env_str,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("STARIO_TEST_"):
            monkeypatch.delenv(key, raising=False)


def test_env_str_unset_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert env_str("STARIO_TEST_STR", "fallback") == "fallback"


def test_env_str_whitespace_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARIO_TEST_STR", "   ")
    assert env_str("STARIO_TEST_STR", "fallback") == "fallback"


def test_env_int_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARIO_TEST_INT", "nope")
    with pytest.raises(ValueError, match="must be an integer"):
        env_int("STARIO_TEST_INT", 1)


def test_env_bool_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARIO_TEST_BOOL", "yes")
    assert env_bool("STARIO_TEST_BOOL", False) is True
    monkeypatch.setenv("STARIO_TEST_BOOL", "off")
    assert env_bool("STARIO_TEST_BOOL", True) is False


def test_env_octal_mode_parses_bare_octal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARIO_TEST_MODE", "640")
    assert env_octal_mode("STARIO_TEST_MODE", 0o600) == 0o640


def test_env_path_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARIO_TEST_PATH", "   ")
    with pytest.raises(ValueError, match="must not be empty"):
        env_path("STARIO_TEST_PATH", "/tmp/default")
