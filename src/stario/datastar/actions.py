"""Declarative fetch strings (``@get`` / ``@post`` / …) for ``data-on-*`` attributes.

Docstring examples that build a tag use ``h = stario.html`` (same as attribute helpers).
``#`` HTML lines are ``stario.html.render(...)`` output where applicable.

**Typing:** ``ContentType``, ``RequestCancellation``, and ``Retry`` are ``typing.Literal``
aliases for fetch options—use them in ``TypedDict``s, ``Protocol``s, or wrappers around
``get``/``post``/… instead of duplicating string unions.
"""

from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlencode

from .format import FilterValue, js, parse_filter_value, s

ContentType = Literal["json", "form"]
RequestCancellation = Literal["auto", "disabled"]
Retry = Literal["auto", "error", "always", "never"]


def peek(callable_expr: str) -> str:
    """``@peek(expr)`` — read a value in an action string without subscribing.

    ```python
    ds.peek("JSON.stringify($cart)")
    # → ``@peek(JSON.stringify($cart))`` (embedded in ``data-on:*`` alongside other actions)
    ```
    """
    return f"@peek({callable_expr})"


def set_all(
    value: str,
    include: FilterValue | None = None,
    exclude: FilterValue | None = None,
) -> str:
    """``@setAll`` — bulk-assign signals (optional include/exclude regex list).

    ```python
    h.Button(ds.on("click", ds.set_all("null", include=["draft", "attachments"])))
    # <button data-on:click="@setAll(null, {&#x27;include&#x27;:&#x27;draft|attachments&#x27;})"></button>
    ```
    """
    if include is not None or exclude is not None:
        filter_dict: dict[str, Any] = {}
        if include is not None:
            filter_dict["include"] = s(parse_filter_value(include))
        if exclude is not None:
            filter_dict["exclude"] = s(parse_filter_value(exclude))
        return f"@setAll({value}, {js(filter_dict)})"
    return f"@setAll({value})"


def toggle_all(
    include: FilterValue | None = None,
    exclude: FilterValue | None = None,
) -> str:
    """``@toggleAll`` — flip booleans that match filters.

    ```python
    h.Button(ds.on("click", ds.toggle_all(include=["showSidebar", "showHelp"])))
    # <button data-on:click="@toggleAll({&#x27;include&#x27;:&#x27;showSidebar|showHelp&#x27;})"></button>
    ```
    """
    if include is not None or exclude is not None:
        filter_dict: dict[str, Any] = {}
        if include is not None:
            filter_dict["include"] = s(parse_filter_value(include))
        if exclude is not None:
            filter_dict["exclude"] = s(parse_filter_value(exclude))
        return f"@toggleAll({js(filter_dict)})"
    return "@toggleAll()"


def _build_fetch(
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
        options.append(f"contentType: {s(str(content_type))}")

    if include is not None or exclude is not None:
        filter_dict: dict[str, Any] = {}
        if include is not None:
            filter_dict["include"] = s(parse_filter_value(include))
        if exclude is not None:
            filter_dict["exclude"] = s(parse_filter_value(exclude))
        options.append(f"filterSignals: {js(filter_dict)}")

    if selector is not None:
        options.append(f"selector: {s(selector)}")

    if headers is not None:
        headers_dict = {k: s(v) for k, v in headers.items()}
        options.append(f"headers: {js(headers_dict)}")

    if open_when_hidden:
        options.append("openWhenHidden: true")

    if payload is not None:
        options.append(f"payload: {js(payload)}")

    if retry != "auto":
        options.append(f"retry: {s(str(retry))}")

    if retry_interval_ms != 1_000:
        options.append(f"retryInterval: {retry_interval_ms}")

    if retry_scaler != 2.0:
        options.append(f"retryScaler: {retry_scaler}")

    if retry_max_wait_ms != 30_000:
        options.append(f"retryMaxWaitMs: {retry_max_wait_ms}")

    if retry_max_count != 10:
        options.append(f"retryMaxCount: {retry_max_count}")

    if request_cancellation != "auto":
        options.append(f"requestCancellation: {s(str(request_cancellation))}")

    if options:
        return f"@{method}({s(full_url)}, {{{', '.join(options)}}})"
    return f"@{method}({s(full_url)})"


def get(
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
    """``@get`` action: declarative GET with options (queries, signal filters, retry, …).

    ```python
    h.Button(ds.on("click", ds.get("/items", {"q": "$query"})), "Search")
    # <button data-on:click="@get(&#x27;/items?q=%24query&#x27;)">Search</button>
    ```
    """
    return _build_fetch(
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
    """``@post`` — same option surface as ``get``.

    ```python
    h.Form(ds.on("submit", ds.post("/login", payload={"user": "$email"})))
    # <form data-on:submit="@post(&#x27;/login&#x27;, {payload: {&#x27;user&#x27;:$email}})"></form>
    ```
    """
    return _build_fetch(
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
    """``@put`` — same option surface as ``get``.

    ```python
    h.Button(ds.on("click", ds.put("/api/r")), "Go")
    # <button data-on:click="@put(&#x27;/api/r&#x27;)">Go</button>
    ```
    """
    return _build_fetch(
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
    """``@patch`` — same option surface as ``get``.

    ```python
    h.Button(ds.on("click", ds.patch("/api/r")), "Go")
    # <button data-on:click="@patch(&#x27;/api/r&#x27;)">Go</button>
    ```
    """
    return _build_fetch(
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
    """``@delete`` — same option surface as ``get``.

    ```python
    h.Button(ds.on("click", ds.delete("/api/r")), "Go")
    # <button data-on:click="@delete(&#x27;/api/r&#x27;)">Go</button>
    ```
    """
    return _build_fetch(
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


def clipboard(text: str, is_base64: bool = False) -> str:
    """``@clipboard`` — copy a literal (or base64) string client-side.

    ```python
    h.Button(ds.on("click", ds.clipboard("https://stario.dev")))
    # <button data-on:click="@clipboard(&#x27;https://stario.dev&#x27;)"></button>
    ```
    """
    if is_base64:
        return f"@clipboard({s(text)}, true)"
    return f"@clipboard({s(text)})"


def fit(
    v: str,
    old_min: float,
    old_max: float,
    new_min: float,
    new_max: float,
    should_clamp: bool = False,
    should_round: bool = False,
) -> str:
    """``@fit`` — remap a numeric expression from one range to another.

    ```python
    ds.fit("$slider", 0, 100, 0, 1, should_clamp=True)
    # → '@fit($slider, 0, 100, 0, 1, true, false)'
    ```
    """
    return (
        f"@fit({v}, {old_min}, {old_max}, {new_min}, {new_max}, "
        f"{str(should_clamp).lower()}, {str(should_round).lower()})"
    )
