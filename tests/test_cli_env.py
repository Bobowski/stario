"""Tests for STARIO_* server environment configuration."""

import pytest

from stario.cli.env import server_config_from_env
from stario.cli.errors import CliError


def test_server_config_from_env_reads_overrides(monkeypatch) -> None:
    monkeypatch.setenv("STARIO_HOST", "0.0.0.0")
    monkeypatch.setenv("STARIO_PORT", "9000")
    monkeypatch.setenv("STARIO_LOOP", "uvloop")
    monkeypatch.setenv("STARIO_UNIX_SOCKET", "/tmp/stario.sock")
    monkeypatch.setenv("STARIO_GRACEFUL_SHUTDOWN_TIMEOUT", "12.5")
    monkeypatch.setenv("STARIO_REUSE_ADDR", "0")

    config = server_config_from_env()
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.event_loop == "uvloop"
    assert config.unix_socket == "/tmp/stario.sock"
    assert config.graceful_shutdown_timeout == 12.5
    assert config.reuse_addr is False


@pytest.mark.parametrize(
    ("env_name", "value", "fragment"),
    [
        ("STARIO_PORT", "70000", "port must be between"),
        ("STARIO_LOOP", "nope", "STARIO_LOOP"),
        ("STARIO_COMPRESS_ZSTD_LEVEL", "23", "zstd_level"),
        (
            "STARIO_REQUESTS_HEADER_TIMEOUT",
            "0",
            "header_timeout must be greater than 0",
        ),
        ("STARIO_REUSE_ADDR", "maybe", "STARIO_REUSE_ADDR"),
    ],
)
def test_invalid_server_env_raises(monkeypatch, env_name, value, fragment) -> None:
    monkeypatch.setenv(env_name, value)
    with pytest.raises(CliError, match=fragment):
        server_config_from_env()
