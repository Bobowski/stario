"""Tests for obsolete UrlPath compose. Href bind lives in test_route."""

import pytest

from stario import UrlPath
from stario.exceptions import StarioError


class TestUrlPath:
    def test_truediv_returns_urlpath(self):
        assert type(UrlPath("/api") / "users") is UrlPath

    def test_rejects_path_without_leading_slash(self):
        with pytest.raises(StarioError, match="path must start with '/'"):
            UrlPath("hello")

    def test_rejects_host_path_without_leading_slash(self):
        with pytest.raises(StarioError, match="path must start with '/'"):
            UrlPath("v1", host="api.example.com")

    def test_rejects_empty_host(self):
        with pytest.raises(StarioError, match="host must not be empty"):
            UrlPath("/users", host="")

    def test_repr_and_host_case(self):
        path = UrlPath("/h/{house_id}")
        hosted = UrlPath("/v1", host="API.Example.COM")

        assert repr(path) == "UrlPath('/h/{house_id}')"
        assert hosted.href() == "//api.example.com/v1"

    def test_truediv_joins_paths(self):
        api = UrlPath("/api/v1")

        assert (api / "users").href() == "/api/v1/users"
        assert (api / "/users/").href() == "/api/v1/users"

    def test_truediv_preserves_host(self):
        api = UrlPath("/v1", host="api.example.com")

        assert (api / "users").href() == "//api.example.com/v1/users"
        assert (api / "users/{user_id}").href(user_id="42") == (
            "//api.example.com/v1/users/42"
        )

    def test_truediv_preserves_placeholders(self):
        house = UrlPath("/h/{house_id}")

        assert (house / "command").href(house_id="abc") == "/h/abc/command"
        assert (house / "{list_id}").href(house_id="abc", list_id="7") == "/h/abc/7"
