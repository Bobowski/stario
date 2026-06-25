"""Tests for UrlPath URL builders."""

import pytest

from stario.exceptions import StarioError
from stario.routing import UrlPath, normalize_path


class TestNormalizePath:
    def test_canonical_path(self):
        assert normalize_path("") == "/"
        assert normalize_path("users") == "/users"
        assert normalize_path("/users/") == "/users"
        assert normalize_path("/") == "/"
        assert normalize_path("//host/") == "/host"
        assert UrlPath("/users/").href() == "/users"
        assert UrlPath("/v1", host="API.Example.COM").href() == "//api.example.com/v1"


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

    def test_href_appends_query_params(self):
        search = UrlPath("/search")

        assert search.href(query={"q": "stario urls", "page": 2}) == (
            "/search?q=stario+urls&page=2"
        )
        assert search.href(query={"tag": ["python", "web"], "empty": None}) == (
            "/search?tag=python&tag=web"
        )

    def test_href_appends_fragment(self):
        docs = UrlPath("/docs")

        assert docs.href(fragment="install guide") == "/docs#install%20guide"
        assert docs.href(query={"q": "routes"}, fragment="section/one?tab=api") == (
            "/docs?q=routes#section/one?tab=api"
        )

    def test_href_quotes_path_params(self):
        path = UrlPath("/files/{name}")

        assert path.href(name="a b.txt") == "/files/a%20b.txt"

    def test_href_rejects_slash_in_path_wildcard(self):
        path = UrlPath("/files/{name}")

        with pytest.raises(StarioError, match="contains '/'"):
            path.href(name="a/b.txt")

    def test_preserves_slashes_for_catchall_params(self):
        path = UrlPath("/files/{path...}")

        assert path.href(path="docs/read me.txt") == "/files/docs/read%20me.txt"

    def test_rejects_empty_segments_in_catchall_params(self):
        path = UrlPath("/files/{path...}")

        for value in ("/docs", "docs/", "docs//readme.txt"):
            with pytest.raises(StarioError, match="empty path segment"):
                path.href(path=value)

    def test_builds_host_routes_with_params(self):
        path = UrlPath("/users/{user_id}", host="{tenant}.example.com")

        assert path.href(tenant="ACME", user_id="42") == "//acme.example.com/users/42"

    def test_rejects_dot_in_host_wildcard(self):
        path = UrlPath("/users", host="{tenant}.example.com")

        with pytest.raises(StarioError, match=r"contains '\.'"):
            path.href(tenant="acme.eu")

    def test_host_catchall_accepts_dotted_values(self):
        path = UrlPath("/users", host="{tenant...}.example.com")

        assert path.href(tenant="acme.eu") == "//acme.eu.example.com/users"

    def test_href_without_kwargs_raises_for_templated_path(self):
        path = UrlPath("/h/{house_id}")

        with pytest.raises(StarioError, match="UrlPath parameter missing"):
            path.href()
        assert repr(path) == "UrlPath('/h/{house_id}')"

    def test_malformed_placeholder_raises(self):
        with pytest.raises(StarioError, match="Invalid route parameter"):
            UrlPath("/{broken")

    @pytest.mark.parametrize("name", ["class", "for", "def", "return", "async"])
    def test_rejects_python_keyword_param_names(self, name: str):
        with pytest.raises(StarioError, match="Python keyword"):
            UrlPath(f"/items/{{{name}}}")

    def test_duplicate_placeholder_raises(self):
        with pytest.raises(StarioError, match="Duplicate route parameter"):
            UrlPath("/teams/{id}/users/{id}")

    def test_rejects_unknown_params(self):
        path = UrlPath("/h/{house_id}")

        with pytest.raises(StarioError, match="unknown parameter"):
            path.href(house_id="abc", typo="oops")

    def test_static_route_rejects_unknown_params(self):
        home = UrlPath("/")

        with pytest.raises(StarioError, match="unknown parameter"):
            home.href(typo="oops")

    def test_href_accepts_positional_param_mapping(self):
        path = UrlPath("/h/{house_id}")

        assert path.href({"house_id": "abc"}) == "/h/abc"

    def test_href_mapping_allows_reserved_param_names(self):
        path = UrlPath("/{query}/{fragment}")

        assert path.href({"query": "q", "fragment": "top"}, query={"page": 2}) == (
            "/q/top?page=2"
        )

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

    def test_request_splitting(self):
        assert UrlPath.request_host("api.example.com") == (
            "com",
            "example",
            "api",
        )
        assert UrlPath.request_path("/users/42") == ("users", "42")
        assert UrlPath.request_path("/") == ()
