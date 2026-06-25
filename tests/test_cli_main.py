"""Tests for top-level CLI argument validation and dispatch."""

import pytest

from stario.cli.main import main


class TestMainArgumentValidation:
    def test_unknown_command_exits_2(self):
        assert main(["frobnicate"]) == 2

    def test_serve_requires_app_spec(self):
        assert main(["serve"]) == 2

    def test_invalid_server_env_exits_1(self, monkeypatch, capsys):
        monkeypatch.setenv("STARIO_PORT", "70000")
        code = main(["serve", "main:bootstrap"])
        assert code == 1
        assert "port must be between" in capsys.readouterr().err

    @pytest.mark.parametrize("cmd", ["init", "new", "routes"])
    def test_removed_commands_exit_2(self, cmd):
        args = [cmd] if cmd in {"init", "new"} else [cmd, "main:bootstrap"]
        assert main(args) == 2
