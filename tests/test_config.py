"""Tests for HTTP server configuration validation."""

import pytest

from stario.exceptions import StarioError
from stario.http.config import RequestPolicy, ServerConfig


def test_request_policy_rejects_low_pipeline_cap() -> None:
    with pytest.raises(StarioError, match="max_pipelined_requests"):
        RequestPolicy(max_pipelined_requests=0)


def test_server_config_rejects_blank_unix_socket() -> None:
    with pytest.raises(StarioError, match="unix_socket must be a non-empty path"):
        ServerConfig(unix_socket="")


def test_server_config_rejects_blank_tcp_host() -> None:
    with pytest.raises(StarioError, match="host must be non-empty"):
        ServerConfig(host="   ")
