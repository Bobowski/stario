"""Shared redirect encoding used by responses and Datastar SSE."""

import pytest

import stario.responses as responses
from stario.exceptions import RedirectException, StarioError
from stario.http.writer import Writer
from stario.responses import normalized_location
from tests.test_writer import _make_writer

SAFE_REDIRECT_TARGETS = [
    "/dashboard",
    "/dashboard with spaces?tab=team settings#profile section",
    "//api.example.com/v1/users",
]

UNSAFE_REDIRECT_TARGETS = [
    r"/\\evil",
    "/safe\r\nx-header: injected",
    "login",
    "javascript:alert(1)",
    "ftp://example.com/file",
]

UNSAFE_REDIRECT_MATCH = (
    "control characters|backslashes|app-relative path or absolute"
)


def _redirect_from_exception(w: Writer, exc: RedirectException) -> None:
    """Same path as App's default RedirectException on_error handler."""
    responses.redirect(w, exc.location, exc.status_code)


class TestNormalizedLocation:
    @pytest.mark.parametrize("target", SAFE_REDIRECT_TARGETS)
    def test_accepts_safe_targets(self, target: str) -> None:
        assert normalized_location(target)

    def test_accepts_url_path_host_href(self) -> None:
        from stario.routing import UrlPath

        target = UrlPath("/users/{user_id}", host="{tenant}.example.com").href(
            tenant="acme", user_id="42"
        )
        assert normalized_location(target) == target

    @pytest.mark.parametrize("target", UNSAFE_REDIRECT_TARGETS)
    def test_rejects_unsafe_targets(self, target: str) -> None:
        with pytest.raises(StarioError, match=UNSAFE_REDIRECT_MATCH):
            normalized_location(target)


class TestRedirectParity:
    def test_direct_and_exception_handler_paths_agree_on_safe_target(self) -> None:
        target = "/dashboard"
        direct, sink_direct, loop_direct = _make_writer()
        via_handler, sink_handler, loop_handler = _make_writer()
        try:
            responses.redirect(direct, target, 302)
            _redirect_from_exception(via_handler, RedirectException(302, target))

            direct_bytes = bytes(sink_direct)
            handler_bytes = bytes(sink_handler)
            assert direct_bytes == handler_bytes
        finally:
            loop_direct.close()
            loop_handler.close()

    def test_redirect_rejects_non_3xx_status(self) -> None:
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="3xx"):
                responses.redirect(w, "/ok", 200)
        finally:
            loop.close()
