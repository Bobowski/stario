from brotli_asgi import BrotliMiddleware as AsgiBrotliMiddleware
from starlette.middleware import Middleware
from starlette.types import ASGIApp


class BrotliMiddleware(AsgiBrotliMiddleware):

    def __init__(
        self,
        app: ASGIApp,
        quality: int = 4,
        mode: str = "text",
        lgwin: int = 22,
        lgblock: int = 0,
        minimum_size: int = 400,
        gzip_fallback: bool = True,
        excluded_handlers: list | None = None,
    ) -> None:
        """
        Arguments.

        mode: The compression mode can be:
            generic, text (*default*. Used for UTF-8 format text input)
            or font (for WOFF 2.0).
        quality: Controls the compression-speed vs compression-
            density tradeoff. The higher the quality, the slower the compression.
            Range is 0 to 11.
        lgwin: Base 2 logarithm of the sliding window size. Range
            is 10 to 24.
        lgblock: Base 2 logarithm of the maximum input block size.
            Range is 16 to 24. If set to 0, the value will be set based on the
            quality.
        minimum_size: Only compress responses that are bigger than this value in bytes.
        gzip_fallback: If True, uses gzip encoding if br is not in the Accept-Encoding header.
        excluded_handlers: List of handlers to be excluded from being compressed.
        """
        # fmt: off
        super().__init__(
            app               = app,
            quality           = quality,
            mode              = mode,
            lgwin             = lgwin,
            lgblock           = lgblock,
            minimum_size      = minimum_size,
            gzip_fallback     = gzip_fallback,
            excluded_handlers = excluded_handlers,
        )
        # fmt: on

    @classmethod
    def as_middleware(
        cls,
        quality: int = 4,
        mode: str = "text",
        lgwin: int = 22,
        lgblock: int = 0,
        minimum_size: int = 400,
        gzip_fallback: bool = True,
        excluded_handlers: list | None = None,
    ) -> Middleware:

        # fmt: off
        return Middleware(
            cls,
            quality           = quality,
            mode              = mode,
            lgwin             = lgwin,
            lgblock           = lgblock,
            minimum_size      = minimum_size,
            gzip_fallback     = gzip_fallback,
            excluded_handlers = excluded_handlers,
        )
        # fmt: on
