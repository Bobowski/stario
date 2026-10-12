"""Build the request a test asked for: URL, headers, cookies, encoded body."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from stario.http.headers import Headers
from stario.testing.cookies import serialize_cookie_header
from stario.testing.encode import encode_request_body, expand_pairs
from stario.testing.types import (
    CookieMap,
    FileData,
    FormData,
    HeaderMap,
    QueryParamInput,
)


def prepare_request(
    *,
    method: str,
    url: str,
    base_url: str,
    base_headers: Headers,
    request_headers: HeaderMap | None,
    client_cookies: CookieMap,
    request_cookies: CookieMap | None,
    params: QueryParamInput | None,
    json: Any | None,
    data: FormData | bytes | str | None,
    files: FileData | None,
    content: bytes | str | None,
) -> tuple[str, str, str, str, Headers, bytes]:
    headers = Headers()
    merge_headers(headers, base_headers)
    merge_headers(headers, request_headers)

    if json is not None and any(value is not None for value in (data, files, content)):
        raise ValueError("Use only one of `json`, `data`, `files`, or `content`.")
    if files is not None and content is not None:
        raise ValueError("Use `files` with optional form `data`, not raw `content`.")

    parsed = urlsplit(urljoin(base_url + "/", url.lstrip("/")))
    # Wire form: keep existing %XX escapes, encode anything else a client would.
    path = quote(parsed.path or "/", safe="/%!$&'()*+,;=:@~")
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if params is not None:
        query_items.extend(expand_pairs(params))
    query_string = urlencode(query_items, doseq=True)
    full_url = urlunsplit((parsed.scheme, parsed.netloc, path, query_string, ""))

    body, content_type = encode_request_body(
        json=json,
        data=data,
        files=files,
        content=content,
    )
    if content_type is not None and "content-type" not in headers:
        headers.set("content-type", content_type)
    if body:
        headers.set("content-length", str(len(body)))
    else:
        headers.remove("content-length")

    headers.setdefault("host", parsed.netloc)
    merged_cookies = dict(client_cookies)
    if request_cookies:
        merged_cookies.update(request_cookies)
    if merged_cookies:
        headers.set("cookie", serialize_cookie_header(merged_cookies))

    return method.upper(), full_url, path, query_string, headers, body


def merge_headers(target: Headers, incoming: HeaderMap | None) -> None:
    """Merge fields into `target`; later values replace earlier ones for the same name."""

    if incoming is None:
        return
    for name, value in incoming.items():
        target.set(name, value)
