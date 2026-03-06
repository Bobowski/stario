"""
Stario application routing and request handling.
"""

import asyncio
from functools import lru_cache
from typing import Any

from stario.exceptions import HttpException, SignalValidationError, StarioError
from stario.http.types import Context

from .router import Router
from .types import ErrorHandler
from .writer import Writer

# =============================================================================
# Application
# =============================================================================


class Stario(Router):
    """HTTP application: routing and request handling."""

    def __init__(self) -> None:
        super().__init__()
        self._error_handlers: dict[type[Exception], ErrorHandler[Any]] = {
            HttpException: lambda c, w, exc: exc.respond(w),
            SignalValidationError: lambda c, w, exc: w.text("Invalid signals", 422),
        }

        # Host-based routing
        self._hosts_exact: dict[str, Router] = {}
        self._hosts_wildcard: list[tuple[str, Router]] = []  # (suffix, router)

        @lru_cache(maxsize=64)
        def find_handler(exc_type: type[Exception]) -> ErrorHandler[Any] | None:
            for t in exc_type.__mro__:
                if t is Exception:
                    return None
                if handler := self._error_handlers.get(t):
                    return handler
            return None

        self._find_error_handler = find_handler

    def on_error(
        self, exc_type: type[Exception], handler: ErrorHandler[Exception]
    ) -> None:
        """Register custom error handler for exception type."""
        self._error_handlers[exc_type] = handler
        self._find_error_handler.cache_clear()

    # =========================================================================
    # Host-based routing
    # =========================================================================

    def host(self, pattern: str, router: Router) -> None:
        """
        Route requests to a router based on Host header.

        Supports exact matches and wildcard prefixes:
        - "api.example.com" - exact match
        - "*.example.com" - wildcard, sets request.subhost to matched portion

        Precedence: exact hosts first, then wildcards (longest suffix first),
        then fallback to routes registered directly on the app.

        Example:
            api = Router()
            api.get("/users", list_users)
            app.host("api.example.com", api)

            tenant = Router()
            tenant.get("/dashboard", dashboard)
            app.host("*.example.com", tenant)  # request.subhost = "acme"

        Host matching is checked before path routing. Routes registered
        directly on the app act as fallback for unmatched hosts.
        """
        pattern = pattern.lower()

        # Reject bare "*" - users should use fallback routes instead
        if pattern == "*":
            raise StarioError(
                "Invalid host pattern: '*'",
                context={"pattern": pattern},
                help_text="Use '*.domain.com' for wildcard subdomains. "
                "Routes registered directly on the app serve as fallback for unmatched hosts.",
            )

        if pattern.startswith("*."):
            suffix = pattern[1:]  # "*.example.com" -> ".example.com"
            # Check for duplicate wildcard
            for existing_suffix, _ in self._hosts_wildcard:
                if existing_suffix == suffix:
                    raise StarioError(
                        f"Wildcard host already registered: {pattern}",
                        context={"pattern": pattern},
                        help_text="Each wildcard pattern can only have one router.",
                    )
            self._hosts_wildcard.append((suffix, router))
            # Longest suffix first for most specific match
            self._hosts_wildcard.sort(key=lambda x: len(x[0]), reverse=True)
        else:
            if pattern in self._hosts_exact:
                raise StarioError(
                    f"Host already registered: {pattern}",
                    context={"pattern": pattern},
                    help_text="Each host pattern can only have one router.",
                )
            self._hosts_exact[pattern] = router

    async def dispatch(self, c: Context, w: Writer) -> None:
        """Dispatch request, checking host routing first."""
        # Fast path: skip if no host routing configured
        if self._hosts_exact or self._hosts_wildcard:
            host = c.req.host

            # O(1) exact match
            if router := self._hosts_exact.get(host):
                await router.dispatch(c, w)
                return

            # Wildcard match (typically 1-3 patterns)
            for suffix, router in self._hosts_wildcard:
                if host.endswith(suffix):
                    c.req.subhost = host[: -len(suffix)]
                    await router.dispatch(c, w)
                    return

        # Fallback to regular path routing
        await Router.dispatch(self, c, w)

    async def handle_request(self, c: Context, w: Writer) -> None:
        """Handle request with tracing and error handling."""

        span = c.span
        span.start()
        span.attr("request.method", c.req.method)
        span.attr("request.path", c.req.path)

        try:
            await self.dispatch(c, w)
        except Exception as exc:
            handled = False
            if not w.started:
                if handler := self._find_error_handler(type(exc)):
                    try:
                        result = handler(c, w, exc)
                        if asyncio.iscoroutine(result):
                            await result
                        handled = True
                    except Exception:
                        pass
                if not handled:
                    w.text("Internal Server Error", 500)
            if not handled:
                span.fail(str(exc))
                span.exception(exc)
        finally:
            w.end()
            span.attr("response.status_code", w._status_code)
            span.end()
