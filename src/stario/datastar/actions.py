"""
Datastar action string builders.

Independent builders for Datastar action strings. No app integration -
just pure string generation. Users provide URLs and methods directly.

Reference: https://data-star.dev/reference/actions
"""

from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlencode

from .format import FilterValue, js, parse_filter_value, s

ContentType = Literal["json", "form"]
RequestCancellation = Literal["auto", "disabled"]
Retry = Literal["auto", "error", "always", "never"]

class DatastarActions:
    """
    Generator for Datastar action strings.

    All methods are pure string builders - no app integration needed.

    Usage:
        from stario.datastar import at

        button({"data-on-click": at.get("/api/users")})
        button({"data-on-click": at.post("/api/submit", {"id": 123})})
    """

    def peek(self, callable_expr: str) -> str:
        """Peek at a value without triggering reactivity."""
        return f"@peek({callable_expr})"

    def set_all(
        self,
        value: str,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> str:
        """Set all matching signals to a value."""
        if include is not None or exclude is not None:
            filter_dict: dict[str, Any] = {}
            if include is not None:
                filter_dict["include"] = s(parse_filter_value(include))
            if exclude is not None:
                filter_dict["exclude"] = s(parse_filter_value(exclude))
            return f"@setAll({value}, {js(filter_dict)})"
        return f"@setAll({value})"

    def toggle_all(
        self,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> str:
        """Toggle all matching boolean signals."""
        if include is not None or exclude is not None:
            filter_dict: dict[str, Any] = {}
            if include is not None:
                filter_dict["include"] = s(parse_filter_value(include))
            if exclude is not None:
                filter_dict["exclude"] = s(parse_filter_value(exclude))
            return f"@toggleAll({js(filter_dict)})"
        return "@toggleAll()"

    def _build_fetch(
        self,
        method: str,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        payload: dict[str, Any] | None = None,
        retry: Retry | str = "auto",
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """Build a fetch action string."""
        full_url = f"{url}?{urlencode(queries)}" if queries else url

        options: list[str] = []

        if content_type != "json":
            options.append(f"contentType: '{content_type}'")

        if include is not None or exclude is not None:
            filter_dict: dict[str, Any] = {}
            if include is not None:
                filter_dict["include"] = s(parse_filter_value(include))
            if exclude is not None:
                filter_dict["exclude"] = s(parse_filter_value(exclude))
            options.append(f"filterSignals: {js(filter_dict)}")

        if selector is not None:
            options.append(f"selector: '{selector}'")

        if headers is not None:
            headers_dict = {k: s(v) for k, v in headers.items()}
            options.append(f"headers: {js(headers_dict)}")

        if open_when_hidden:
            options.append("openWhenHidden: true")

        if payload is not None:
            options.append(f"payload: {js(payload)}")

        if retry != "auto":
            options.append(f"retry: '{retry}'")

        if retry_interval_ms != 1_000:
            options.append(f"retryInterval: {retry_interval_ms}")

        if retry_scaler != 2.0:
            options.append(f"retryScaler: {retry_scaler}")

        if retry_max_wait_ms != 30_000:
            options.append(f"retryMaxWaitMs: {retry_max_wait_ms}")

        if retry_max_count != 10:
            options.append(f"retryMaxCount: {retry_max_count}")

        if request_cancellation != "auto":
            options.append(f"requestCancellation: '{request_cancellation}'")

        if options:
            return f"@{method}('{full_url}', {{{', '.join(options)}}})"
        return f"@{method}('{full_url}')"

    def get(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        payload: dict[str, Any] | None = None,
        retry: Retry | str = "auto",
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """Generate a GET fetch action."""
        return self._build_fetch(
            "get",
            url,
            queries,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            payload=payload,
            retry=retry,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def post(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        payload: dict[str, Any] | None = None,
        retry: Retry | str = "auto",
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """Generate a POST fetch action."""
        return self._build_fetch(
            "post",
            url,
            queries,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            payload=payload,
            retry=retry,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def put(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        payload: dict[str, Any] | None = None,
        retry: Retry | str = "auto",
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """Generate a PUT fetch action."""
        return self._build_fetch(
            "put",
            url,
            queries,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            payload=payload,
            retry=retry,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def patch(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        payload: dict[str, Any] | None = None,
        retry: Retry | str = "auto",
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """Generate a PATCH fetch action."""
        return self._build_fetch(
            "patch",
            url,
            queries,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            payload=payload,
            retry=retry,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    def delete(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str = "json",
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool = False,
        payload: dict[str, Any] | None = None,
        retry: Retry | str = "auto",
        retry_interval_ms: int = 1_000,
        retry_scaler: float = 2.0,
        retry_max_wait_ms: int = 30_000,
        retry_max_count: int = 10,
        request_cancellation: RequestCancellation | str = "auto",
    ) -> str:
        """Generate a DELETE fetch action."""
        return self._build_fetch(
            "delete",
            url,
            queries,
            content_type=content_type,
            include=include,
            exclude=exclude,
            selector=selector,
            headers=headers,
            open_when_hidden=open_when_hidden,
            payload=payload,
            retry=retry,
            retry_interval_ms=retry_interval_ms,
            retry_scaler=retry_scaler,
            retry_max_wait_ms=retry_max_wait_ms,
            retry_max_count=retry_max_count,
            request_cancellation=request_cancellation,
        )

    # Pro Actions (require Datastar Pro)
    def clipboard(self, text: str, is_base64: bool = False) -> str:
        """Copy text to clipboard."""
        if is_base64:
            return f"@clipboard('{text}', true)"
        return f"@clipboard('{text}')"

    def fit(
        self,
        v: str,
        old_min: float,
        old_max: float,
        new_min: float,
        new_max: float,
        should_clamp: bool = False,
        should_round: bool = False,
    ) -> str:
        """Map a value from one range to another."""
        return (
            f"@fit({v}, {old_min}, {old_max}, {new_min}, {new_max}, "
            f"{str(should_clamp).lower()}, {str(should_round).lower()})"
        )
