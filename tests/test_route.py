"""Tests for Route — method plus UrlPath."""

import pytest

from stario.exceptions import StarioError
from stario.routing import Route, UrlPath


class TestRoute:
    def test_factories_set_method_and_path(self):
        home = Route.get("/")
        search = Route.query("/feed")
        send = Route.post("/rooms/{room_id}/send")

        assert home.method == "GET"
        assert home.path.path_text == "/"
        assert search.method == "QUERY"
        assert send.method == "POST"
        assert send.href(room_id="abc") == "/rooms/abc/send"

    def test_accepts_urlpath_and_preserves_host(self):
        api = UrlPath("/users", host="api.example.com")
        users = Route.get(api)

        assert users.href() == "//api.example.com/users"
        assert users.path is api

    def test_constructor_canonicalizes_method(self):
        route = Route(" patch ", "/items")

        assert route.method == "PATCH"
        assert Route("PROPFIND", "/dav") == Route("propfind", "/dav")

    def test_rejects_empty_or_spaced_method(self):
        with pytest.raises(StarioError, match="HTTP token"):
            Route("", "/")
        with pytest.raises(StarioError, match="HTTP token"):
            Route("GET POST", "/")

    def test_href_matches_urlpath(self):
        path = UrlPath("/h/{house_id}")
        route = Route.post(path)

        assert route.href(house_id="x", query={"edit": "1"}) == path.href(
            house_id="x", query={"edit": "1"}
        )

    def test_equality_is_method_and_pattern(self):
        assert Route.get("/") == Route.get("/")
        assert Route.get("/") != Route.post("/")
        assert Route.get(UrlPath("/users")) == Route.get("/users")
        assert {Route.get("/"), Route.post("/")} == {Route.post("/"), Route.get("/")}

    def test_repr_shows_method_and_path(self):
        assert (
            repr(Route.delete("/log/event")) == "Route('DELETE', UrlPath('/log/event'))"
        )
