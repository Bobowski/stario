"""Public re-exports from ``stario`` package root."""


def test_http_exception_alias_matches_exceptions_module() -> None:
    from stario import HttpException
    from stario.exceptions import HttpException as HttpExceptionDefined

    assert HttpException is HttpExceptionDefined


def test_redirect_exception_alias_matches_exceptions_module() -> None:
    from stario import RedirectException
    from stario.exceptions import RedirectException as RedirectExceptionDefined

    assert RedirectException is RedirectExceptionDefined
