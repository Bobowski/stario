"""In-process async HTTP test client."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin

from stario.http.app import App
from stario.http.bootstrap import Bootstrap
from stario.http.compression import CompressionConfig
from stario.http.headers import Headers
from stario.testing.cookies import merge_cookie_jar
from stario.testing.dispatch import (
    merge_headers,
    prepare_request,
    run_dispatch,
    wire_dispatch,
)
from stario.testing.encode import (
    decode_content_encoding,
    parse_http_response,
    parse_response_cookies,
)
from stario.testing.load import aload_app
from stario.testing.models import ClientRequest
from stario.testing.response import TestResponse, TestStreamResponse
from stario.testing.tracer import TestTracer
from stario.testing.transport import GrowingSink, try_parse_http_head, wait_sink
from stario.testing.types import (
    CookieMap,
    FileData,
    FormData,
    HeaderMap,
    QueryParamInput,
)


@dataclass(slots=True, frozen=True)
class TestExchange:
    __test__ = False

    request: ClientRequest
    response: TestResponse


class TestClient:
    """Async HTTP client: exercise an `App` in-process.

    Pass a fully wired app or the same bootstrap async generator your program
    uses in production. Always enter `async with TestClient(...)` before calling
    `request` / `stream` / …; then `app` is the live application.

    Buffered requests — `request` waits for the entire response body and returns
    `TestResponse`. The `get` / `head` / … helpers are thin wrappers with the
    same keyword arguments. Redirects are followed up to `max_redirects` unless
    overridden per call.

    Streaming — `stream` provides `TestStreamResponse` after headers; it does
    not follow redirects. Prefer `Accept-Encoding: identity` when using
    `TestStreamResponse.iter_bytes`. `timeout` applies to the full exchange
    (headers and body reads). Leaving the `stream` block disconnects
    that exchange and awaits the handler.

    Telemetry — each response exposes `span_id`; finished data is on `tracer`.
    Buffered calls append `TestExchange` rows to `exchanges`.

    Exit — buffered exchanges are disconnected, `drain_tasks` runs, then
    bootstrap teardown (if any) matches normal app shutdown.
    """

    __test__ = False

    def __init__(
        self,
        app_or_bootstrap: App | Bootstrap,
        *,
        app_factory: Callable[[], App] | None = None,
        owns_shutdown: bool = True,
        base_url: str = "http://testserver",
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        follow_redirects: bool = True,
        max_redirects: int = 20,
        compression: CompressionConfig | None = None,
        request_timeout: float | None = 30.0,
    ) -> None:
        """Wire the client.

        - `app_or_bootstrap`: Built `App` or production bootstrap async generator.
        - `app_factory`: Optional factory when using a bootstrap (default `App`).
        - `owns_shutdown`: When passing a built `App`, whether exiting the client
          signals `app.shutdown` (default `True`). Set `False` when an outer
          `aload_app` or `Server` owns the app's shutdown future.
        - `base_url`: Origin for relative URLs (default `http://testserver`).
        - `headers`: Default headers merged into every request.
        - `cookies`: Initial cookie jar (`dict`-like).
        - `follow_redirects`: Default for buffered requests; ignored by `stream`.
        - `max_redirects`: Buffered redirect cap.
        - `compression`: Passed to the synthetic `Writer`.
        - `request_timeout`: Default seconds per request; `None` means no timeout.
        """
        if isinstance(app_or_bootstrap, App):
            self._app = app_or_bootstrap
            self._bootstrap = None
            self._app_factory = None
            self._owns_shutdown = owns_shutdown
        else:
            self._app = None
            self._bootstrap = app_or_bootstrap
            self._app_factory = app_factory
            self._owns_shutdown = False
        self.base_url = base_url.rstrip("/") or "http://testserver"
        self.cookies: dict[str, str] = dict(cookies or {})
        self.default_follow_redirects = follow_redirects
        self.default_headers = Headers()
        merge_headers(self.default_headers, headers)
        self.default_headers.setdefault("user-agent", "stario-testclient")
        self.default_headers.setdefault("accept", "*/*")
        self.exchanges: list[TestExchange] = []
        self.max_redirects = max_redirects
        self.tracer = TestTracer()
        self.compression = (
            compression if compression is not None else CompressionConfig()
        )
        self.request_timeout = request_timeout
        self._async_app_cm: AbstractAsyncContextManager[App] | None = None
        self._entered = False

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("TestClient is already entered.")
        self.tracer.__enter__()
        try:
            if self._bootstrap is not None:
                self._async_app_cm = aload_app(
                    self._bootstrap, app_factory=self._app_factory, tracer=self.tracer
                )
                self._app = await self._async_app_cm.__aenter__()
            self._entered = True
            return self
        except BaseException:
            self.tracer.__exit__(None, None, None)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            for ex in self.exchanges:
                fut = ex.response.disconnect
                if not fut.done():
                    fut.set_result(None)
            if self._app is not None:
                if self._async_app_cm is None and self._owns_shutdown:
                    self._app.signal_shutdown()
                await self.drain_tasks()
            if self._async_app_cm is not None:
                await self._async_app_cm.__aexit__(exc_type, exc_val, exc_tb)
                self._async_app_cm = None
                self._app = None
        finally:
            self._entered = False
            self.tracer.__exit__(exc_type, exc_val, exc_tb)

    async def drain_tasks(self) -> None:
        """Wait until `App.drain_tasks` is quiet and `tracer` has no open spans.

        Called automatically after signalling disconnect when exiting the client context.
        Unsafe to call from work scheduled via `app.create_task` (can deadlock).
        """

        if not self._entered:
            raise RuntimeError(
                "TestClient must be entered before drain_tasks(). Use `async with TestClient(...)`."
            )
        if self._app is None:
            raise RuntimeError(
                "TestClient has no wired app (enter `async with TestClient(...)` before drain_tasks)."
            )
        while True:
            await self._app.drain_tasks()
            if not self.tracer.has_open_spans():
                return
            await asyncio.sleep(0)

    @property
    def app(self) -> App:
        """The running `App` (available inside `async with TestClient(...)`)."""

        if not self._entered:
            raise RuntimeError(
                "TestClient must be used as an async context manager. "
                "Use `async with TestClient(...) as client:` before calling request helpers."
            )
        if self._app is None:
            raise RuntimeError(
                "TestClient was given a bootstrap function but is not inside `async with TestClient(...)`. "
                "Enter the context manager before calling await .get() / .post() / …."
            )
        return self._app

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None = None,
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        json: Any | None = None,
        data: FormData | bytes | str | None = None,
        files: FileData | None = None,
        content: bytes | str | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[TestStreamResponse]:
        """Start a request and yield `TestStreamResponse` once headers are available.

        Does not follow redirects (inspect `Location` yourself). On context exit,
        signals client disconnect for this exchange and awaits the handler coroutine.
        `timeout` caps the whole exchange, including body iteration after headers.
        Use `Accept-Encoding: identity` when reading `iter_bytes` so the body is not compressed.
        """

        pm, purl, ppath, pqs, phdrs, pbody = prepare_request(
            method=method,
            url=url,
            base_url=self.base_url,
            base_headers=self.default_headers,
            request_headers=headers,
            client_cookies=self.cookies,
            request_cookies=cookies,
            params=params,
            json=json,
            data=data,
            files=files,
            content=content,
        )

        sink = GrowingSink()
        wired = wire_dispatch(
            app=self.app,
            tracer=self.tracer,
            pm=pm,
            purl=purl,
            ppath=ppath,
            pqs=pqs,
            phdrs=phdrs,
            pbody=pbody,
            compression=self.compression,
            transport_write=sink.extend,
        )
        disconnect = wired.disconnect
        timeout_secs = self.request_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        exchange_deadline = (
            None if timeout_secs is None else loop.time() + timeout_secs
        )

        async def run_stream() -> None:
            await run_dispatch(
                self.app,
                wired,
                deadline=timeout_secs,
                on_finished=sink.mark_app_done,
            )

        app_task = asyncio.create_task(run_stream(), name="stario.testclient.stream")

        try:
            seen = sink.gen
            parsed = try_parse_http_head(sink.buf)
            while parsed is None:
                if sink.app_done:
                    await app_task
                    raise RuntimeError(
                        "Application exited before response headers were sent."
                    )
                await wait_sink(sink, seen, deadline=exchange_deadline)
                seen = sink.gen
                parsed = try_parse_http_head(sink.buf)

            status_code, response_headers, body_start = parsed
            response_cookies = parse_response_cookies(response_headers)
            merge_cookie_jar(self.cookies, response_headers)
            client_request = wired.client_request

            te = (response_headers.get("transfer-encoding") or "").lower()
            chunked = "chunked" in te
            cl_raw = response_headers.get("content-length")
            content_length: int | None = None
            if cl_raw is not None:
                try:
                    content_length = int(cl_raw)
                except ValueError:
                    content_length = None

            st = TestStreamResponse(
                status_code=status_code,
                url=purl,
                headers=response_headers,
                request=client_request,
                span_id=wired.root_span.id,
                cookies=response_cookies,
                sink=sink,
                _body_start=body_start,
                _chunked=chunked,
                _content_length=content_length,
                _disconnect_future=disconnect,
                _app_task=app_task,
                _deadline=exchange_deadline,
            )
            yield st
        finally:
            if not disconnect.done():
                disconnect.set_result(None)
            await app_task

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None = None,
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        json: Any | None = None,
        data: FormData | bytes | str | None = None,
        files: FileData | None = None,
        content: bytes | str | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> TestResponse:
        """Issue an arbitrary HTTP method and return a fully buffered `TestResponse`."""

        return await self._request(
            method,
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            json=json,
            data=data,
            files=files,
            content=content,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    # --- Shortcuts: same `**kwargs` as `request` ---

    async def get(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("HEAD", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("DELETE", url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> TestResponse:
        return await self.request("OPTIONS", url, **kwargs)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None = None,
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        json: Any | None = None,
        data: FormData | bytes | str | None = None,
        files: FileData | None = None,
        content: bytes | str | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> TestResponse:
        redirect_mode = (
            self.default_follow_redirects
            if follow_redirects is None
            else follow_redirects
        )
        current_method = method.upper()
        current_url = url
        current_params = params
        current_json = json
        current_data = data
        current_files = files
        current_content = content
        current_headers = headers
        current_cookies = dict(cookies) if cookies is not None else None
        history: list[TestResponse] = []

        for _ in range(self.max_redirects + 1):
            response = await self._send_once(
                current_method,
                current_url,
                params=current_params,
                headers=current_headers,
                cookies=current_cookies,
                json=current_json,
                data=current_data,
                files=current_files,
                content=current_content,
                timeout=timeout,
            )
            response.history = list(history)
            if not redirect_mode or not response.is_redirect:
                return response

            history.append(response)
            current_url = urljoin(response.url, response.headers.get("location", ""))
            current_params = None
            nh = Headers()
            merge_headers(nh, current_headers)
            current_headers = nh

            if response.status_code in {301, 302, 303} and current_method != "HEAD":
                current_method = "GET"
                current_json = None
                current_data = None
                current_files = None
                current_content = None
                current_headers.remove("content-type")
                current_headers.remove("content-length")
            if current_cookies is not None:
                updated_cookies = dict(current_cookies)
                merge_cookie_jar(updated_cookies, response.headers)
                current_cookies = updated_cookies

        raise RuntimeError(
            f"Too many redirects. Exceeded configured limit of {self.max_redirects}."
        )

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        params: QueryParamInput | None = None,
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        json: Any | None = None,
        data: FormData | bytes | str | None = None,
        files: FileData | None = None,
        content: bytes | str | None = None,
        timeout: float | None = None,
    ) -> TestResponse:
        pm, purl, ppath, pqs, phdrs, pbody = prepare_request(
            method=method,
            url=url,
            base_url=self.base_url,
            base_headers=self.default_headers,
            request_headers=headers,
            client_cookies=self.cookies,
            request_cookies=cookies,
            params=params,
            json=json,
            data=data,
            files=files,
            content=content,
        )

        sink = bytearray()
        wired = wire_dispatch(
            app=self.app,
            tracer=self.tracer,
            pm=pm,
            purl=purl,
            ppath=ppath,
            pqs=pqs,
            phdrs=phdrs,
            pbody=pbody,
            compression=self.compression,
            transport_write=sink.extend,
        )
        deadline = self.request_timeout if timeout is None else timeout
        await run_dispatch(self.app, wired, deadline=deadline)

        status_code, response_headers, response_body = parse_http_response(bytes(sink))
        response_body = decode_content_encoding(
            response_body,
            response_headers.get("content-encoding"),
        )
        response_cookies = parse_response_cookies(response_headers)
        merge_cookie_jar(self.cookies, response_headers)
        client_request = wired.client_request
        response = TestResponse(
            status_code=status_code,
            url=purl,
            headers=response_headers,
            content=response_body,
            request=client_request,
            span_id=wired.root_span.id,
            cookies=response_cookies,
            _disconnect_future=wired.disconnect,
        )
        self.exchanges.append(
            TestExchange(
                request=client_request,
                response=response,
            )
        )
        return response
