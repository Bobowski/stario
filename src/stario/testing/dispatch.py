"""Wire synthetic HTTP requests into an in-process `App` dispatch."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from stario.http.app import App
from stario.http.compression import CompressionConfig
from stario.http.context import Context
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request
from stario.http.writer import Writer
from stario.telemetry.core import Span
from stario.testing.cookies import serialize_cookie_header
from stario.testing.encode import (
    date_header,
    encode_request_body,
    expand_pairs,
)
from stario.testing.models import ClientRequest
from stario.testing.tracer import TestTracer
from stario.testing.transport import MemoryTransport
from stario.testing.types import (
    CookieMap,
    FileData,
    FormData,
    HeaderMap,
    QueryParamInput,
)


@dataclass(slots=True)
class WiredDispatch:
    ctx: Context
    writer: Writer
    disconnect: asyncio.Future[None]
    root_span: Span
    client_request: ClientRequest


def wire_dispatch(
    *,
    app: App,
    tracer: TestTracer,
    pm: str,
    purl: str,
    ppath: str,
    pqs: str,
    phdrs: Headers,
    pbody: bytes,
    compression: CompressionConfig,
    transport_write: Callable[[bytes], None],
) -> WiredDispatch:
    loop = asyncio.get_running_loop()
    disconnect = loop.create_future()
    root_span = tracer.create(pm)
    request = _make_request(
        method=pm,
        path=ppath,
        query_string=pqs,
        headers=phdrs,
        body=pbody,
        disconnect=disconnect,
    )
    writer = Writer(
        transport=MemoryTransport(transport_write),
        get_date_header=date_header,
        on_completed=lambda: None,
        compression=compression,
        accept_encoding=phdrs.get("accept-encoding"),
    )
    ctx = Context(
        app=app,
        req=request,
        span=root_span,
        state={},
        _disconnect=disconnect,
    )
    return WiredDispatch(
        ctx=ctx,
        writer=writer,
        disconnect=disconnect,
        root_span=root_span,
        client_request=ClientRequest(
            method=pm,
            url=purl,
            path=ppath,
            query_string=pqs,
            headers=phdrs,
            content=pbody,
        ),
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
    path = parsed.path or "/"
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
    """Merge fields into ``target``; later values replace earlier ones for the same name."""

    if incoming is None:
        return
    for name, value in incoming.items():
        target.set(name, value)


async def run_dispatch(
    app: App,
    wired: WiredDispatch,
    *,
    deadline: float | None,
    on_finished: Callable[[BaseException | None], None] | None = None,
) -> None:
    """Run one in-process request; always signals ``wired.disconnect`` when done."""

    exc: BaseException | None = None
    try:
        coro = app(wired.ctx, wired.writer)
        if deadline is None:
            await coro
        else:
            await asyncio.wait_for(coro, timeout=deadline)
    except BaseException as e:
        exc = e
        if isinstance(e, TimeoutError) and not wired.disconnect.done():
            wired.disconnect.set_result(None)
        raise
    finally:
        if on_finished is not None:
            on_finished(exc)
        if not wired.disconnect.done():
            wired.disconnect.set_result(None)


def _make_request(
    *,
    method: str,
    path: str,
    query_string: str,
    headers: Headers,
    body: bytes,
    disconnect: asyncio.Future[None] | None = None,
) -> Request:
    reader = BodyReader(
        pause=lambda: None,
        resume=lambda: None,
        disconnect=disconnect,
    )
    if body:
        reader.feed(body)
    reader.complete()
    return Request(
        method=method,
        path=path,
        query_bytes=query_string.encode("ascii"),
        headers=headers,
        body=reader,
    )
