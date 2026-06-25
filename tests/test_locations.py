"""Tests for URL path normalization and query/fragment helpers."""

from stario.routing.locations import append_query_fragment, normalize_path


class TestNormalizePath:
    def test_canonical_path(self):
        assert normalize_path("") == "/"
        assert normalize_path("users") == "/users"
        assert normalize_path("/users/") == "/users"
        assert normalize_path("/") == "/"

    def test_collapses_leading_slashes(self):
        assert normalize_path("//host/") == "/host"
        assert normalize_path("///") == "/"


class TestAppendQueryFragment:
    def test_appends_query(self):
        assert append_query_fragment("/search", query={"q": "stario", "page": 2}) == (
            "/search?q=stario&page=2"
        )

    def test_skips_none_query_values(self):
        assert append_query_fragment("/search", query={"q": "x", "empty": None}) == (
            "/search?q=x"
        )

    def test_skips_empty_query(self):
        assert append_query_fragment("/search", query={}) == "/search"
        assert append_query_fragment("/search", query={"empty": None}) == "/search"

    def test_repeated_query_keys(self):
        assert append_query_fragment("/search", query={"tag": ["python", "web"]}) == (
            "/search?tag=python&tag=web"
        )

    def test_appends_fragment(self):
        assert append_query_fragment("/docs", fragment="install guide") == (
            "/docs#install%20guide"
        )

    def test_appends_query_and_fragment(self):
        assert append_query_fragment(
            "/docs",
            query={"q": "routes"},
            fragment="section/one?tab=api",
        ) == "/docs?q=routes#section/one?tab=api"

    def test_empty_fragment(self):
        assert append_query_fragment("/docs", fragment="") == "/docs#"
