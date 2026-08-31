"""Constructor contracts for stario.exceptions."""

import pytest

from stario.exceptions import (
    RedirectException,
    RequestBodyError,
    StarioError,
)


class TestStarioError:
    def test_context_is_copied_from_caller_dict(self):
        ctx = {"k": 1}
        exc = StarioError("msg", context=ctx)
        ctx["k"] = 2
        assert exc.context == {"k": 1}


class TestRequestBodyError:
    @pytest.mark.parametrize("status", [404, 500, 422])
    def test_rejects_non_body_status_at_construction(self, status: int):
        with pytest.raises(StarioError, match="requires status 408 or 413"):
            RequestBodyError(status, "nope")

    def test_accepts_408_and_413(self):
        RequestBodyError(413, "too large")
        RequestBodyError(408, "stalled")


class TestRedirectException:
    def test_rejects_non_3xx_status_at_construction(self):
        with pytest.raises(StarioError, match="requires a 3xx"):
            RedirectException(404, "/nope")
