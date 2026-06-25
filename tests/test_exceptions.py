"""Constructor contracts for stario.exceptions."""

import pytest

from stario.exceptions import (
    HttpException,
    RedirectException,
    StarioError,
)


class TestStarioError:
    def test_context_is_copied_from_caller_dict(self):
        ctx = {"k": 1}
        exc = StarioError("msg", context=ctx)
        ctx["k"] = 2
        assert exc.context == {"k": 1}


class TestHttpException:
    @pytest.mark.parametrize("status", [200, 302])
    def test_rejects_non_error_status_at_construction(self, status: int):
        with pytest.raises(StarioError, match="requires a 4xx or 5xx"):
            HttpException(status, "nope")

    def test_accepts_4xx_and_5xx_status(self):
        HttpException(404, "missing")
        HttpException(500, "broken")


class TestRedirectException:
    def test_rejects_non_3xx_status_at_construction(self):
        with pytest.raises(StarioError, match="requires a 3xx"):
            RedirectException(404, "/nope")
