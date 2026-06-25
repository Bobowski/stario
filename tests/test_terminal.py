"""Tests for stario._terminal."""

import sys

import pytest

from stario._terminal import enable_windows_console_vt


def test_enable_windows_console_vt_is_safe_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    enable_windows_console_vt()
    enable_windows_console_vt()
