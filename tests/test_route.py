"""Tests for Route — method plus path. Lexer errors live in test_segment."""

import pytest

from stario.exceptions import StarioError
from stario.http.route import EMPTY_ROUTE, Route


class TestRoute:
    def test_one_string_and_two_args(self):
        home = Route("GET /")
        search = Route("QUERY /feed")
        send = Route("POST", "/rooms/{room_id}/send")

        assert home.method == "GET"
        assert home.path == "/"
        assert search.method == "QUERY"
        assert send.method == "POST"
        assert send.pattern == "POST /rooms/{room_id}/send"
        assert send.target == "/rooms/{room_id}/send"
        assert home == Route("GET", "/")

    def test_parses_host_from_network_path(self):
        users = Route("GET //api.example.com/users")

        assert users.pattern == "GET //api.example.com/users"
        assert Route("GET /users", host="api.example.com") == users

    def test_rejects_host_twice(self):
        with pytest.raises(StarioError, match="Host given twice"):
            Route("GET //api.example.com/users", host="other.example.com")

    def test_rejects_path_without_slash(self):
        with pytest.raises(StarioError, match="must start with '/'"):
            Route("GET", "users")
        with pytest.raises(StarioError, match="Host must not be empty"):
            Route("GET ///users")

    def test_rejects_spec_without_path(self):
        with pytest.raises(StarioError, match="needs a path"):
            Route("GET")

    def test_keyword_parts(self):
        listed = Route(method="GET", path="/users/{id}")
        hosted = Route(method="GET", path="/users", host="api.example.com")
        hosted_parts = Route(
            method="GET",
            path=hosted.path,
            host=hosted.host,
        )

        assert hosted == Route("GET //api.example.com/users")
        assert hosted_parts == hosted
        assert listed.path == "/users/{id}"
        assert Route("/api").method == ""
        assert Route("/api").path == "/api"

    def test_rejects_piece_given_twice(self):
        with pytest.raises(StarioError, match="Method given twice"):
            Route("GET /home", method="GET")
        with pytest.raises(StarioError, match="Path given twice"):
            Route("POST", "/rooms/{room_id}", path="/send")

    def test_rejects_query_or_fragment_on_constructor(self):
        with pytest.raises(StarioError, match="does not store query or fragment"):
            Route("GET /search?q=1")
        with pytest.raises(StarioError, match="does not store query or fragment"):
            Route("GET /page#a")
        with pytest.raises(StarioError, match="does not store query or fragment"):
            Route(path="/search?q=1")

    def test_constructor_canonicalizes_method(self):
        route = Route(" patch ", "/items")

        assert route.method == "PATCH"
        assert Route("PROPFIND", "/dav") == Route("propfind", "/dav")

    def test_rejects_empty_or_spaced_method(self):
        with pytest.raises(StarioError, match="HTTP token"):
            Route("", "/")
        with pytest.raises(StarioError, match="HTTP token"):
            Route(method="GET POST", path="/")
        with pytest.raises(StarioError, match="must start with '/'"):
            Route("GET POST", "/")

    def test_equality_is_method_host_and_path(self):
        send = Route("POST /rooms/{room_id}/send")

        assert Route("GET /") == Route("GET /")
        assert Route("GET /") != Route("POST /")
        assert send == Route("POST /rooms/{room_id}/send")
        assert send != Route("POST /rooms/abc/send")
        assert {Route("GET /"), Route("POST /")} == {Route("POST /"), Route("GET /")}

    def test_repr_is_the_address(self):
        assert repr(Route("DELETE /log/event")) == "Route('DELETE /log/event')"

    def test_empty_sentinel_has_no_address(self):
        assert EMPTY_ROUTE.path == ""
        assert EMPTY_ROUTE.host is None
        assert EMPTY_ROUTE.target == ""
        assert EMPTY_ROUTE.pattern == ""
        assert Route.empty() is EMPTY_ROUTE

    def test_route_is_immutable(self):
        home = Route("GET /")

        with pytest.raises(AttributeError, match="immutable"):
            home.path = "/other"

    def test_stores_host_and_path_strings(self):
        users = Route("GET //API.Example.COM/users/{id}")

        assert users.host == "api.example.com"
        assert users.path == "/users/{id}"
        assert users.target == "//api.example.com/users/{id}"
        assert users.pattern == "GET //api.example.com/users/{id}"


class TestRouteHref:
    users = Route("GET /t/{tenant}/users/{id}")
    hosted = Route("GET /users/{id}", host="{tenant}.example.com")
    send = Route("POST /rooms/{room_id}/send")
    files = Route("GET /files/{path...}")
    home = Route("GET /")

    def test_all_positional_and_all_keyword(self):
        assert self.users.href("acme", 42) == "/t/acme/users/42"
        assert self.users.href(tenant="acme", id=42) == "/t/acme/users/42"
        assert self.users.href(id=42, tenant="acme") == "/t/acme/users/42"

    def test_mixed_positional_then_keyword(self):
        assert self.users.href("acme", id=42) == "/t/acme/users/42"
        assert self.hosted.href("acme", id=42) == "//acme.example.com/users/42"

    def test_positionals_are_host_then_path(self):
        assert self.hosted.href("acme", 7) == "//acme.example.com/users/7"
        # Swapped positionals bind in order, not by value kind.
        assert self.hosted.href(7, "acme") == "//7.example.com/users/acme"

    def test_missing_all_or_some(self):
        with pytest.raises(StarioError, match="parameter missing"):
            self.users.href()
        with pytest.raises(StarioError, match="parameter missing"):
            self.users.href("acme")
        with pytest.raises(StarioError, match="parameter missing"):
            self.users.href(tenant="acme")
        with pytest.raises(StarioError, match="parameter missing"):
            self.users.href(id=42)
        with pytest.raises(StarioError, match="parameter missing"):
            self.hosted.href(tenant="acme")
        with pytest.raises(StarioError, match="parameter missing"):
            self.send.href(query={"ok": "1"})

    def test_too_many_positionals(self):
        with pytest.raises(StarioError, match="too many positional"):
            self.send.href("abc", "extra")
        with pytest.raises(StarioError, match="too many positional"):
            self.users.href("acme", 42, "extra")
        with pytest.raises(StarioError, match="too many positional"):
            self.home.href("nope")

    def test_unknown_keyword(self):
        with pytest.raises(StarioError, match="unknown parameter"):
            self.send.href(room_id="abc", extra="x")
        with pytest.raises(StarioError, match="unknown parameter"):
            self.users.href("acme", 42, extra="x")
        with pytest.raises(StarioError, match="unknown parameter"):
            self.home.href(typo="x")

    def test_same_placeholder_twice(self):
        with pytest.raises(StarioError, match="more than once"):
            self.send.href("abc", room_id="abc")
        with pytest.raises(StarioError, match="more than once"):
            self.users.href("acme", tenant="acme", id=42)

    def test_rejects_none_and_empty(self):
        with pytest.raises(StarioError, match="must not be None"):
            self.send.href(None)
        with pytest.raises(StarioError, match="must not be empty"):
            self.send.href("")
        with pytest.raises(StarioError, match="must not be None"):
            self.hosted.href(tenant="acme", id=None)
        with pytest.raises(StarioError, match="must not be empty"):
            self.hosted.href(tenant="", id=1)

    def test_rejects_mapping_positional(self):
        with pytest.raises(StarioError, match="must not be a mapping"):
            self.send.href({"room_id": "abc"})

    def test_query_and_fragment_stay_off_the_filler(self):
        assert self.send.href("abc", query={"ok": "1"}) == "/rooms/abc/send?ok=1"
        assert self.send.href(room_id="abc", fragment="top") == "/rooms/abc/send#top"
        assert self.home.href(query={"q": "x"}, fragment="a") == "/?q=x#a"

    def test_wildcard_and_catchall_values(self):
        assert self.send.href(3) == "/rooms/3/send"
        with pytest.raises(StarioError, match="contains '/'"):
            Route("GET /files/{name}").href("a/b")
        assert self.files.href("docs/a b.txt") == "/files/docs/a%20b.txt"
        for value in ("/docs", "docs/", "docs//x"):
            with pytest.raises(StarioError, match="empty path segment"):
                self.files.href(value)

    def test_host_value_rules(self):
        with pytest.raises(StarioError, match=r"contains '\.'"):
            self.hosted.href("acme.eu", 1)
        with pytest.raises(StarioError, match="invalid character"):
            self.hosted.href("acme/eu", 1)
        with pytest.raises(StarioError, match="invalid character"):
            self.hosted.href("evil?", 1)
        catchall = Route("GET /", host="{tenant...}.example.com")
        assert catchall.href("acme.eu") == "//acme.eu.example.com/"
        with pytest.raises(StarioError, match="empty host label"):
            catchall.href("acme.")

    def test_empty_route_cannot_build_href(self):
        with pytest.raises(StarioError, match="unmatched route"):
            EMPTY_ROUTE.href()
        with pytest.raises(StarioError, match="unmatched route"):
            EMPTY_ROUTE.href("x")

    def test_zero_and_false_are_values(self):
        assert self.send.href(0) == "/rooms/0/send"
        assert self.send.href(False) == "/rooms/False/send"

    def test_escaped_braces_are_literal(self):
        route = Route("GET /curly/{{id}}")
        mixed = Route("GET /curly/{{x}}/{id}")
        hosted = Route("GET /", host="{{API}}.example.com")

        assert route.pattern == "GET /curly/{id}"
        assert route.href() == "/curly/{id}"
        assert mixed.href("7") == "/curly/{x}/7"
        assert hosted.host == "{api}.example.com"
        assert hosted.href() == "//{api}.example.com/"
        with pytest.raises(StarioError, match="too many positional"):
            route.href("abc")
