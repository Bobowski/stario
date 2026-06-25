"""Datastar action strings.

Use the exported `at` instance:

```python
from stario.datastar import at, data

h.Button(data.on("click", at.get("/items")), "Refresh")
```

The methods return strings such as `@get('/items')` for use inside Datastar
attributes like `data.on(...)`.
"""

from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlencode

from stario.exceptions import StarioError

from .format import FilterValue, filter_js, js_object, string_literal

ContentType = Literal["json", "form"]
RequestCancellation = Literal["auto", "cleanup", "disabled"]
Retry = Literal["auto", "error", "always", "never"]
IntlType = Literal[
    "datetime", "number", "pluralRules", "relativeTime", "list", "displayNames"
]


class DatastarActions:
    """Namespace for Datastar `@...` action strings."""

    __slots__ = ()

    def peek(self, callable_expr: str) -> str:
        """Build `@peek(expr)` to read a value without subscribing."""
        return f"@peek({callable_expr})"

    def set_all(
        self,
        value: str,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> str:
        """Build `@setAll(...)` to assign matching signals in bulk."""
        filters = filter_js(include, exclude)
        if filters is not None:
            return f"@setAll({value}, {filters})"
        return f"@setAll({value})"

    def toggle_all(
        self,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
    ) -> str:
        """Build `@toggleAll(...)` to flip matching boolean signals."""
        filters = filter_js(include, exclude)
        if filters is not None:
            return f"@toggleAll({filters})"
        return "@toggleAll()"

    def _fetch(
        self,
        method: str,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool | None = None,
        payload: dict[str, Any] | None = None,
        retry: Retry | str | None = None,
        retry_interval_ms: int | None = None,
        retry_scaler: float | None = None,
        retry_max_wait_ms: int | None = None,
        retry_max_count: int | None = None,
        request_cancellation: RequestCancellation | str | None = None,
    ) -> str:
        """Build a Datastar backend fetch action.

        Values in `payload` are JavaScript expressions. Wrap literal text with
        `string_literal()` so it is emitted as a JS string.
        """
        if selector is not None and content_type != "form":
            raise StarioError(
                "selector is only used with content_type='form'",
                context={"selector": selector, "content_type": str(content_type)},
                help_text=(
                    "The fetch `selector` option picks which <form> to submit; pass "
                    "content_type='form' alongside it, or drop the selector."
                ),
            )

        options: list[str] = []

        if content_type is not None:
            options.append(f"contentType: {string_literal(str(content_type))}")

        filters = filter_js(include, exclude)
        if filters is not None:
            options.append(f"filterSignals: {filters}")

        if selector is not None:
            options.append(f"selector: {string_literal(selector)}")

        if headers is not None:
            headers_dict = {k: string_literal(v) for k, v in headers.items()}
            options.append(f"headers: {js_object(headers_dict)}")

        if open_when_hidden is not None:
            options.append(f"openWhenHidden: {str(open_when_hidden).lower()}")

        if payload is not None:
            options.append(f"payload: {js_object(payload)}")

        if retry is not None:
            options.append(f"retry: {string_literal(str(retry))}")

        if retry_interval_ms is not None:
            options.append(f"retryInterval: {retry_interval_ms}")

        if retry_scaler is not None:
            options.append(f"retryScaler: {retry_scaler}")

        if retry_max_wait_ms is not None:
            options.append(f"retryMaxWait: {retry_max_wait_ms}")

        if retry_max_count is not None:
            options.append(f"retryMaxCount: {retry_max_count}")

        if request_cancellation is not None:
            options.append(
                f"requestCancellation: {string_literal(str(request_cancellation))}"
            )

        full_url = url
        if queries:
            query_string = urlencode(queries, doseq=True)
            if query_string:
                separator = (
                    "" if url.endswith(("?", "&")) else "&" if "?" in url else "?"
                )
                full_url = url + separator + query_string
        if options:
            return f"@{method}({string_literal(full_url)}, {{{', '.join(options)}}})"
        return f"@{method}({string_literal(full_url)})"

    def get(
        self,
        url: str,
        queries: Mapping[str, Any] | None = None,
        *,
        content_type: ContentType | str | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool | None = None,
        payload: dict[str, Any] | None = None,
        retry: Retry | str | None = None,
        retry_interval_ms: int | None = None,
        retry_scaler: float | None = None,
        retry_max_wait_ms: int | None = None,
        retry_max_count: int | None = None,
        request_cancellation: RequestCancellation | str | None = None,
    ) -> str:
        """Build `@get(...)`.

        ```python
        h.Button(data.on("click", at.get("/items", {"q": "$query"})), "Search")
        ```
        """
        return self._fetch(
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
        content_type: ContentType | str | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool | None = None,
        payload: dict[str, Any] | None = None,
        retry: Retry | str | None = None,
        retry_interval_ms: int | None = None,
        retry_scaler: float | None = None,
        retry_max_wait_ms: int | None = None,
        retry_max_count: int | None = None,
        request_cancellation: RequestCancellation | str | None = None,
    ) -> str:
        """Build `@post(...)` with the same option surface as `get`."""
        return self._fetch(
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
        content_type: ContentType | str | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool | None = None,
        payload: dict[str, Any] | None = None,
        retry: Retry | str | None = None,
        retry_interval_ms: int | None = None,
        retry_scaler: float | None = None,
        retry_max_wait_ms: int | None = None,
        retry_max_count: int | None = None,
        request_cancellation: RequestCancellation | str | None = None,
    ) -> str:
        """Build `@put(...)` with the same option surface as `get`."""
        return self._fetch(
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
        content_type: ContentType | str | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool | None = None,
        payload: dict[str, Any] | None = None,
        retry: Retry | str | None = None,
        retry_interval_ms: int | None = None,
        retry_scaler: float | None = None,
        retry_max_wait_ms: int | None = None,
        retry_max_count: int | None = None,
        request_cancellation: RequestCancellation | str | None = None,
    ) -> str:
        """Build `@patch(...)` with the same option surface as `get`."""
        return self._fetch(
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
        content_type: ContentType | str | None = None,
        include: FilterValue | None = None,
        exclude: FilterValue | None = None,
        selector: str | None = None,
        headers: dict[str, str] | None = None,
        open_when_hidden: bool | None = None,
        payload: dict[str, Any] | None = None,
        retry: Retry | str | None = None,
        retry_interval_ms: int | None = None,
        retry_scaler: float | None = None,
        retry_max_wait_ms: int | None = None,
        retry_max_count: int | None = None,
        request_cancellation: RequestCancellation | str | None = None,
    ) -> str:
        """Build `@delete(...)` with the same option surface as `get`."""
        return self._fetch(
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

    def clipboard(self, text: str, is_base64: bool = False) -> str:
        """Build `@clipboard(...)`. **Datastar Pro only.**"""
        if is_base64:
            return f"@clipboard({string_literal(text)}, true)"
        return f"@clipboard({string_literal(text)})"

    def intl(
        self,
        type: IntlType | str,
        value: str,
        options: dict[str, Any] | None = None,
        locale: str | list[str] | None = None,
    ) -> str:
        """Build `@intl(...)`. **Datastar Pro only.**

        `value` is a JavaScript expression. String values inside `options` are
        emitted as JavaScript string literals; numbers and booleans pass through.
        """
        intl_type = type
        action = f"@intl({string_literal(str(intl_type))}, {value}"
        if options is not None:
            quoted = {
                k: string_literal(v) if isinstance(v, str) else v
                for k, v in options.items()
            }
            action += f", {js_object(quoted)}"
        elif locale is not None:
            action += ", undefined"
        if locale is not None:
            if isinstance(locale, str):
                action += f", {string_literal(locale)}"
            else:
                action += (
                    ", [" + ", ".join(string_literal(item) for item in locale) + "]"
                )
        return action + ")"

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
        """Build `@fit(...)` to remap a numeric expression. **Datastar Pro only.**"""
        return (
            f"@fit({v}, {old_min}, {old_max}, {new_min}, {new_max}, "
            f"{str(should_clamp).lower()}, {str(should_round).lower()})"
        )


at = DatastarActions()
