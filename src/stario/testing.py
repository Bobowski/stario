"""HTTP testing: in-process client with no open port.

Public surface is mainly ``TestClient``, ``TestResponse``, ``TestStreamResponse``,
and ``TestTracer``.
Tests need an async runner (for example pytest-asyncio).
"""

import asyncio
import bisect
import http.cookies
import json as json_module
import time
import zlib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from compression import zstd
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from http import HTTPStatus
from typing import Any, AsyncGenerator, Literal, Self
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import UUID, uuid7

import brotli

from stario.http.app import App
from stario.http.bootstrap import BootstrapCandidate, normalize_bootstrap
from stario.http.context import Context
from stario.http.headers import Headers
from stario.http.request import BodyReader, Request
from stario.http.writer import CompressionConfig, Writer
from stario.telemetry.core import Span

type QueryScalar = str | int | float | bool
type QueryValue = QueryScalar | Sequence[QueryScalar]
type QueryParamInput = Mapping[str, QueryValue] | Sequence[tuple[str, QueryScalar]]
type HeaderMap = Mapping[str, str] | Headers
type CookieMap = Mapping[str, str]
type FormValue = str | int | float | bool
type FormData = Mapping[str, FormValue | Sequence[FormValue]] | Sequence[tuple[str, FormValue]]
type FileValue = (
    bytes
    | str
    | tuple[str, bytes | str]
    | tuple[str, bytes | str, str]
)
type FileData = Mapping[str, FileValue] | Sequence[tuple[str, FileValue]]


@dataclass(slots=True, frozen=True)
class TelemetryEvent:
    name: str
    time_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    body: Any | None = None


@dataclass(slots=True, frozen=True)
class TelemetryLink:
    span_id: UUID
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class TelemetrySpan:
    id: UUID
    name: str
    parent_id: UUID | None
    start_ns: int
    end_ns: int
    status: str
    error: str | None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: tuple[TelemetryEvent, ...] = ()
    links: tuple[TelemetryLink, ...] = ()

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True, frozen=True)
class ClientRequest:
    method: str
    url: str
    path: str
    query_string: str
    headers: Headers
    content: bytes

    @property
    def body(self) -> bytes:
        return self.content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json_module.loads(self.content)


@dataclass(slots=True)
class TestResponse:
    """Buffered HTTP result from ``TestClient.get``, ``TestClient.post``, etc.

    The body is fully buffered; ``Content-Encoding`` is decoded when present.
    Pair ``span_id`` with ``TestClient.tracer`` for assertions.
    """

    __test__ = False

    status_code: int
    url: str
    headers: Headers
    content: bytes
    request: ClientRequest
    span_id: UUID
    _disconnect_future: asyncio.Future[None] = field(repr=False)
    history: list["TestResponse"] = field(default_factory=list)
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        charset = "utf-8"
        if ct := self.headers.get("content-type"):
            for part in ct.split(";")[1:]:
                key, _, value = part.strip().partition("=")
                if key.lower() == "charset" and value:
                    charset = value.strip()
                    break
        return self.content.decode(charset, errors="replace")

    def json(self) -> Any:
        return json_module.loads(self.content)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def reason_phrase(self) -> str:
        try:
            return HTTPStatus(self.status_code).phrase
        except ValueError:
            return ""

    @property
    def is_redirect(self) -> bool:
        return (
            self.status_code in {301, 302, 303, 307, 308}
            and self.headers.get("location") is not None
        )

    def raise_for_status(self) -> Self:
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} {self.reason_phrase}: {self.text}")
        return self


class _GrowingSink:
    __slots__ = ("buf", "_gen", "_event", "_app_done", "_app_exc")

    def __init__(self) -> None:
        self.buf = bytearray()
        self._gen = 0
        self._event = asyncio.Event()
        self._app_done = False
        self._app_exc: BaseException | None = None

    def extend(self, data: bytes) -> None:
        if data:
            self.buf.extend(data)
            self._gen += 1
            self._event.set()

    def mark_app_done(self, exc: BaseException | None = None) -> None:
        self._app_done = True
        self._app_exc = exc
        self._gen += 1
        self._event.set()

    @property
    def gen(self) -> int:
        return self._gen

    async def wait(self, seen_gen: int) -> None:
        while self._gen == seen_gen and not self._app_done:
            await self._event.wait()
            self._event.clear()
        if self._app_exc is not None:
            raise self._app_exc


def _try_parse_http_head(buf: bytes) -> tuple[int, Headers, int] | None:
    head, sep, _ = buf.partition(b"\r\n\r\n")
    if not sep:
        return None
    header_end = len(head) + 4
    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ", 2)
    if len(status_parts) < 2:
        raise RuntimeError("Malformed test response: invalid status line.")
    status_code = int(status_parts[1])
    headers = Headers()
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers.radd(name.lower(), value.lstrip())
    return status_code, headers, header_end


@dataclass(slots=True)
class TestStreamResponse:
    """Streaming result from ``TestClient.stream`` once response headers exist.

    Use ``body``, ``iter_bytes``, or ``iter_events`` to read the entity body.
    Leaving the ``stream`` context disconnects this exchange and awaits the app task.
    """

    __test__ = False

    status_code: int
    url: str
    headers: Headers
    request: ClientRequest
    span_id: UUID
    cookies: dict[str, str]
    _sink: _GrowingSink
    _body_start: int
    _chunked: bool
    _content_length: int | None
    _disconnect_future: asyncio.Future[None]
    _app_task: asyncio.Task[None]

    async def body(self) -> bytes:
        """Concatenate all body chunks after transfer decoding (same as iterating ``iter_bytes``)."""

        parts: list[bytes] = []
        async for chunk in self.iter_bytes():
            parts.append(chunk)
        return b"".join(parts)

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """Yield decoded body chunks as they arrive (chunked / fixed-length / until close).

        Only ``identity`` ``Content-Encoding`` is supported; request
        ``Accept-Encoding: identity`` or use buffered methods for compression.
        """

        ce = (self.headers.get("content-encoding") or "").strip().lower()
        if ce and ce != "identity":
            raise RuntimeError(
                "TestClient.iter_bytes does not support Content-Encoding="
                f"{ce!r}; use accept-encoding that yields identity or use buffered client.get()."
            )

        sink = self._sink
        pos = self._body_start

        if self.status_code in {204, 304} or self.request.method == "HEAD":
            return

        if self._chunked:
            while True:
                nl = sink.buf.find(b"\r\n", pos)
                while nl < 0:
                    if sink._app_done:
                        raise RuntimeError("Incomplete chunked encoding in stream.")
                    seen = sink.gen
                    await sink.wait(seen)
                    nl = sink.buf.find(b"\r\n", pos)
                size_line = bytes(sink.buf[pos:nl])
                pos = nl + 2
                chunk_size = int(size_line.split(b";", 1)[0], 16)
                if chunk_size == 0:
                    return
                end_data = pos + chunk_size
                while len(sink.buf) < end_data + 2:
                    if sink._app_done and len(sink.buf) < end_data:
                        raise RuntimeError("Truncated chunked body in stream.")
                    seen = sink.gen
                    await sink.wait(seen)
                payload = bytes(sink.buf[pos:end_data])
                pos = end_data + 2
                if payload:
                    yield payload

        elif self._content_length is not None:
            remain = self._content_length
            while remain > 0:
                avail = len(sink.buf) - pos
                while avail <= 0:
                    if sink._app_done:
                        raise RuntimeError("Truncated fixed-length body in stream.")
                    seen = sink.gen
                    await sink.wait(seen)
                    avail = len(sink.buf) - pos
                take = min(avail, remain)
                yield bytes(sink.buf[pos : pos + take])
                pos += take
                remain -= take

        else:
            while True:
                avail = len(sink.buf) - pos
                while avail <= 0 and not sink._app_done:
                    seen = sink.gen
                    await sink.wait(seen)
                    avail = len(sink.buf) - pos
                if avail > 0:
                    yield bytes(sink.buf[pos:])
                    pos = len(sink.buf)
                if sink._app_done:
                    return

    async def iter_events(self) -> AsyncIterator[dict[str, str]]:
        """Parse ``text/event-stream`` into dicts (keys such as ``event``, ``id``, ``data``)."""

        buffer = b""
        async for chunk in self.iter_bytes():
            buffer += chunk
            while True:
                idx = buffer.find(b"\r\n\r\n")
                sep_len = 4
                if idx < 0:
                    idx = buffer.find(b"\n\n")
                    sep_len = 2
                if idx < 0:
                    break
                raw = buffer[:idx]
                buffer = buffer[idx + sep_len :]
                text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
                data_lines: list[str] = []
                ev: dict[str, str] = {}
                for line in text.split("\n"):
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith(" "):
                        if data_lines:
                            data_lines[-1] += line[1:]
                        continue
                    if ":" in line:
                        fld, _, rest = line.partition(":")
                        rest = rest.lstrip(" ")
                    else:
                        fld, rest = line, ""
                    fld = fld.strip()
                    if fld == "data":
                        data_lines.append(rest)
                    elif fld in ("event", "id"):
                        ev[fld] = rest
                if data_lines:
                    ev["data"] = "\n".join(data_lines)
                yield ev


@dataclass(slots=True, frozen=True)
class TestExchange:
    __test__ = False

    request: ClientRequest
    response: TestResponse


@dataclass(slots=True)
class _SpanState:
    id: UUID
    name: str
    parent_id: UUID | None
    start_ns: int = 0
    end_ns: int | None = None
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[TelemetryEvent] = field(default_factory=list)
    links: list[TelemetryLink] = field(default_factory=list)


_UNSET: Any = object()


class TestTracer:
    """Test-side view of telemetry for ``TestClient``.

    Implements the ``stario.telemetry.Tracer`` protocol for request dispatch
    (``create``, ``start``, ``add_event``, ``end``, … — see that protocol for
    handler-time usage). Assertions in tests should use the query helpers below;
    they only return finished span snapshots.
    """

    def __init__(self) -> None:
        self._spans: dict[UUID, _SpanState] = {}
        self._finished: list[TelemetrySpan] = []
        self._finished_by_id: dict[UUID, TelemetrySpan] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def create(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        /,
        *,
        parent_id: UUID | None = None,
    ) -> Span:
        span = Span(id=uuid7(), tracer=self)
        self._spans[span.id] = _SpanState(
            id=span.id,
            name=name,
            parent_id=parent_id,
            attributes=attributes.copy() if attributes else {},
        )
        return span

    def start(self, span_id: UUID) -> None:
        if state := self._spans.get(span_id):
            if state.start_ns == 0:
                state.start_ns = time.time_ns()

    def set_attribute(self, span_id: UUID, name: str, value: Any) -> None:
        if state := self._spans.get(span_id):
            state.attributes[name] = value

    def set_attributes(self, span_id: UUID, attributes: dict[str, Any]) -> None:
        if attributes and (state := self._spans.get(span_id)):
            state.attributes.update(attributes)

    def add_event(
        self,
        span_id: UUID,
        name: str,
        attributes: dict[str, Any] | None = None,
        /,
        *,
        body: Any | None = None,
    ) -> None:
        if state := self._spans.get(span_id):
            state.events.append(
                TelemetryEvent(
                    name=name,
                    time_ns=time.time_ns(),
                    attributes=attributes.copy() if attributes else {},
                    body=body,
                )
            )

    def add_link(
        self,
        span_id: UUID,
        target_span_id: UUID,
        attributes: dict[str, Any] | None = None,
        /,
    ) -> None:
        if state := self._spans.get(span_id):
            state.links.append(
                TelemetryLink(
                    span_id=target_span_id,
                    attributes=attributes.copy() if attributes else {},
                )
            )

    def set_name(self, span_id: UUID, name: str) -> None:
        if state := self._spans.get(span_id):
            state.name = name

    def fail(self, span_id: UUID, message: str) -> None:
        if state := self._spans.get(span_id):
            state.error = message

    def end(self, span_id: UUID) -> None:
        state = self._spans.pop(span_id, None)
        if state is None or state.end_ns is not None:
            return
        if state.start_ns == 0:
            raise RuntimeError(
                "Cannot end a span that was never started. "
                "Call span.start() or use the span as a context manager."
            )
        state.end_ns = time.time_ns()
        span = TelemetrySpan(
            id=state.id,
            name=state.name,
            parent_id=state.parent_id,
            start_ns=state.start_ns,
            end_ns=state.end_ns,
            status="error" if state.error else "ok",
            error=state.error,
            attributes=dict(state.attributes),
            events=tuple(state.events),
            links=tuple(state.links),
        )
        bisect.insort_right(
            self._finished,
            span,
            key=lambda s: (s.start_ns, s.end_ns),
        )
        self._finished_by_id[span.id] = span

    def get_span(self, span_id: UUID) -> TelemetrySpan | None:
        """Return the finished ``TelemetrySpan`` for ``span_id``, or ``None``.

        Open spans and unknown ids yield ``None``.
        """

        return self._finished_by_id.get(span_id)

    def find_span(
        self,
        name: str,
        *,
        root_id: UUID | None = None,
        parent_id: UUID | None = None,
    ) -> TelemetrySpan | None:
        """First finished span named ``name``, in start-time order.

        When several requests reuse the same span name, pass ``root_id=r.span_id``
        (from the matching ``TestResponse``) so you match the right subtree.
        ``parent_id`` requires an exact parent link.
        """

        for s in self._finished:
            if s.name != name:
                continue
            if parent_id is not None and s.parent_id != parent_id:
                continue
            if root_id is not None:
                cur: TelemetrySpan | None = s
                seen: set[UUID] = set()
                under = False
                while cur is not None:
                    if cur.id == root_id:
                        under = True
                        break
                    if cur.id in seen:
                        break
                    seen.add(cur.id)
                    if cur.parent_id is None:
                        break
                    cur = self.get_span(cur.parent_id)
                if not under:
                    continue
            return s
        return None

    def get_events(
        self,
        span_id: UUID,
        *,
        name: str | None = None,
    ) -> tuple[TelemetryEvent, ...]:
        """Events recorded on a finished span; filter with ``name`` when set."""

        span = self.get_span(span_id)
        if span is None:
            return ()
        if name is None:
            return span.events
        return tuple(e for e in span.events if e.name == name)

    def get_event(
        self,
        span_id: UUID,
        event_name: str,
        *,
        index: int = 0,
    ) -> TelemetryEvent | None:
        """The ``index``-th ``TelemetryEvent`` named ``event_name``, or ``None``."""

        matches = [e for e in self.get_events(span_id, name=event_name)]
        if index < 0 or index >= len(matches):
            return None
        return matches[index]

    def has_event(self, span_id: UUID, event_name: str) -> bool:
        """Whether ``get_events(span_id)`` contains at least one ``event_name``."""

        return any(e.name == event_name for e in self.get_events(span_id))

    def has_attribute(
        self,
        span_id: UUID,
        key: str,
        value: Any = _UNSET,
    ) -> bool:
        """Whether the finished span’s attributes include ``key``.

        Pass ``value`` to require equality; omit it to assert presence only.
        """

        span = self.get_span(span_id)
        if span is None:
            return False
        if key not in span.attributes:
            return False
        if value is _UNSET:
            return True
        return span.attributes[key] == value

    @property
    def finished_count(self) -> int:
        """Number of spans that have completed and been recorded."""

        return len(self._finished)

    def has_open_spans(self) -> bool:
        """``True`` while any span created through this tracer has not been ended."""

        return bool(self._spans)


class TestClient:
    """Async HTTP client: exercise an ``App`` in-process.

    Pass a fully wired app or the same bootstrap callable your program uses in
    production. For a bootstrap, enter ``async with TestClient(bootstrap)``
    before calling ``request`` / ``stream`` / …; then ``app`` is the live application.

    Buffered requests — ``request`` waits for the entire response body and returns
    ``TestResponse``. The ``get`` / ``head`` / … helpers are thin wrappers with the
    same keyword arguments. Redirects are followed up to ``max_redirects`` unless
    overridden per call.

    Streaming — ``stream`` provides ``TestStreamResponse`` after headers; it does
    not follow redirects. Prefer ``Accept-Encoding: identity`` when using
    ``TestStreamResponse.iter_bytes``. Leaving the ``stream`` block disconnects
    that exchange and awaits the handler.

    Telemetry — each response exposes ``span_id``; finished data is on ``tracer``.
    Buffered calls append ``TestExchange`` rows to ``exchanges``.

    Exit — buffered exchanges are disconnected, ``drain_tasks`` runs, then
    bootstrap teardown (if any) matches normal app shutdown.
    """

    __test__ = False

    def __init__(
        self,
        app_or_bootstrap: App | BootstrapCandidate,
        *,
        app_factory: Callable[[], App] | None = None,
        base_url: str = "http://testserver",
        headers: HeaderMap | None = None,
        cookies: CookieMap | None = None,
        follow_redirects: bool = True,
        max_redirects: int = 20,
        compression: CompressionConfig = CompressionConfig(),
        request_timeout: float | None = 30.0,
    ) -> None:
        """Wire the client.

        Args:
            app_or_bootstrap: Built ``App`` or production bootstrap (async
                function, async generator, context manager, etc.).
            app_factory: Optional ``lambda: App()`` when using a bootstrap.
            base_url: Origin for relative URLs (default ``http://testserver``).
            headers: Default headers merged into every request.
            cookies: Initial cookie jar (``dict``-like).
            follow_redirects: Default for buffered requests; ignored by ``stream``.
            max_redirects: Buffered redirect cap.
            compression: Passed to the synthetic ``Writer``.
            request_timeout: Default seconds per request; ``None`` means no timeout.
        """
        if isinstance(app_or_bootstrap, App):
            self._app = app_or_bootstrap
            self._bootstrap = None
            self._app_factory = None
        else:
            self._app = None
            self._bootstrap = app_or_bootstrap
            self._app_factory = app_factory
        self.base_url = base_url.rstrip("/") or "http://testserver"
        self.cookies: dict[str, str] = dict(cookies or {})
        self.default_follow_redirects = follow_redirects
        self.default_headers = Headers()
        _merge_headers(self.default_headers, headers)
        self.default_headers.setdefault("user-agent", "stario-testclient")
        self.default_headers.setdefault("accept", "*/*")
        self.exchanges: list[TestExchange] = []
        self.max_redirects = max_redirects
        self.tracer = TestTracer()
        self.compression = compression
        self.request_timeout = request_timeout
        self._async_app_cm: AbstractAsyncContextManager[App] | None = None

    async def __aenter__(self) -> Self:
        if self._bootstrap is not None:
            self._async_app_cm = aload_app(
                self._bootstrap, app_factory=self._app_factory, tracer=self.tracer
            )
            self._app = await self._async_app_cm.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        for ex in self.exchanges:
            fut = ex.response._disconnect_future
            if not fut.done():
                fut.set_result(None)
        if self._app is not None:
            await self.drain_tasks()
        if self._async_app_cm is not None:
            await self._async_app_cm.__aexit__(exc_type, exc_val, exc_tb)
            self._async_app_cm = None
            self._app = None

    async def drain_tasks(self) -> None:
        """Wait until ``App.join_tasks`` is quiet and ``tracer`` has no open spans.

        Called automatically after signalling disconnect when exiting the client context.
        Unsafe to call from work scheduled via ``app.create_task`` (can deadlock).
        """

        if self._app is None:
            raise RuntimeError(
                "TestClient has no wired app (enter `async with TestClient(...)` before drain_tasks)."
            )
        while True:
            await self._app.join_tasks()
            if not self.tracer.has_open_spans():
                return
            await asyncio.sleep(0)

    @property
    def app(self) -> App:
        """The running ``App`` (available after ``async with`` when a bootstrap was passed)."""

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
    ) -> AsyncGenerator[TestStreamResponse, None]:
        """Start a request and yield ``TestStreamResponse`` once headers are available.

        Does not follow redirects (inspect ``Location`` yourself). On context exit,
        signals client disconnect for this exchange and awaits the handler coroutine.
        Use ``Accept-Encoding: identity`` when reading ``iter_bytes`` so the body is not compressed.
        """

        pm, purl, ppath, pqs, phdrs, pbody = _prepare_request(
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

        loop = asyncio.get_running_loop()
        sink = _GrowingSink()
        disconnect = loop.create_future()
        shutdown = loop.create_future()
        app = self.app
        root_span = self.tracer.create(pm)
        request = _make_request(
            method=pm,
            path=ppath,
            query_string=pqs,
            headers=phdrs,
            body=pbody,
            disconnect=disconnect,
        )
        writer = Writer(
            transport_write=sink.extend,
            get_date_header=_date_header,
            on_completed=lambda: None,
            disconnect=disconnect,
            shutdown=shutdown,
            compression=self.compression,
            accept_encoding=phdrs.get("accept-encoding"),
        )
        ctx = Context(app=app, req=request, span=root_span, state={})
        deadline = self.request_timeout if timeout is None else timeout

        async def run_stream() -> None:
            try:
                if deadline is None:
                    await app(ctx, writer)
                else:
                    await asyncio.wait_for(app(ctx, writer), timeout=deadline)
            except TimeoutError as exc:
                sink.mark_app_done(exc)
                if not disconnect.done():
                    disconnect.set_result(None)
                raise
            except BaseException as exc:
                sink.mark_app_done(exc)
                raise
            else:
                sink.mark_app_done(None)

        app_task = asyncio.create_task(run_stream(), name="stario.testclient.stream")

        try:
            seen = sink.gen
            parsed = _try_parse_http_head(bytes(sink.buf))
            while parsed is None:
                if sink._app_done:
                    await app_task
                    raise RuntimeError("Application exited before response headers were sent.")
                if deadline is None:
                    await sink.wait(seen)
                else:
                    await asyncio.wait_for(sink.wait(seen), timeout=deadline)
                seen = sink.gen
                parsed = _try_parse_http_head(bytes(sink.buf))

            status_code, response_headers, body_start = parsed
            response_cookies = _parse_response_cookies(response_headers)
            _merge_cookie_jar(self.cookies, response_headers)
            client_request = ClientRequest(
                method=pm,
                url=purl,
                path=ppath,
                query_string=pqs,
                headers=phdrs,
                content=pbody,
            )

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
                span_id=root_span.id,
                cookies=response_cookies,
                _sink=sink,
                _body_start=body_start,
                _chunked=chunked,
                _content_length=content_length,
                _disconnect_future=disconnect,
                _app_task=app_task,
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
        """Issue an arbitrary HTTP method and return a fully buffered ``TestResponse``."""

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

    # --- Shortcuts: same ``**kwargs`` as ``request`` ---

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
            _merge_headers(nh, current_headers)
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
                _merge_cookie_jar(updated_cookies, response.headers)
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
        pm, purl, ppath, pqs, phdrs, pbody = _prepare_request(
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

        loop = asyncio.get_running_loop()
        sink = bytearray()
        disconnect = loop.create_future()
        shutdown = loop.create_future()
        app = self.app
        root_span = self.tracer.create(pm)
        request = _make_request(
            method=pm,
            path=ppath,
            query_string=pqs,
            headers=phdrs,
            body=pbody,
            disconnect=disconnect,
        )
        writer = Writer(
            transport_write=sink.extend,
            get_date_header=_date_header,
            on_completed=lambda: None,
            disconnect=disconnect,
            shutdown=shutdown,
            compression=self.compression,
            accept_encoding=phdrs.get("accept-encoding"),
        )
        ctx = Context(app=app, req=request, span=root_span, state={})
        deadline = self.request_timeout if timeout is None else timeout
        try:
            if deadline is None:
                await app(ctx, writer)
            else:
                await asyncio.wait_for(app(ctx, writer), timeout=deadline)
        except TimeoutError:
            if not disconnect.done():
                disconnect.set_result(None)
            raise

        status_code, response_headers, response_body = _parse_http_response(bytes(sink))
        response_body = _decode_content_encoding(
            response_body,
            response_headers.get("content-encoding"),
        )
        response_cookies = _parse_response_cookies(response_headers)
        _merge_cookie_jar(self.cookies, response_headers)
        client_request = ClientRequest(
            method=pm,
            url=purl,
            path=ppath,
            query_string=pqs,
            headers=phdrs,
            content=pbody,
        )
        response = TestResponse(
            status_code=status_code,
            url=purl,
            headers=response_headers,
            content=response_body,
            request=client_request,
            span_id=root_span.id,
            cookies=response_cookies,
            _disconnect_future=disconnect,
        )
        self.exchanges.append(
            TestExchange(
                request=client_request,
                response=response,
            )
        )
        return response


def _prepare_request(
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
    _merge_headers(headers, base_headers)
    _merge_headers(headers, request_headers)

    if json is not None and any(value is not None for value in (data, files, content)):
        raise ValueError("Use only one of `json`, `data`, `files`, or `content`.")
    if files is not None and content is not None:
        raise ValueError("Use `files` with optional form `data`, not raw `content`.")

    parsed = urlsplit(urljoin(base_url + "/", url.lstrip("/")))
    path = parsed.path or "/"
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if params is not None:
        query_items.extend(_expand_pairs(params))
    query_string = urlencode(query_items, doseq=True)
    full_url = urlunsplit((parsed.scheme, parsed.netloc, path, query_string, ""))

    body, content_type = _encode_request_body(
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
        headers.set("cookie", _serialize_cookie_header(merged_cookies))

    return method.upper(), full_url, path, query_string, headers, body


def _merge_headers(target: Headers, incoming: HeaderMap | None) -> None:
    if incoming is None:
        return
    if isinstance(incoming, Headers):
        for name, value in incoming.items():
            target.radd(name, value)
        return
    target.update(incoming)


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
    reader._cached = body
    reader._complete = True
    return Request(
        method=method,
        path=path,
        query_bytes=query_string.encode("ascii"),
        headers=headers,
        body=reader,
    )


def _encode_request_body(
    *,
    json: Any | None,
    data: FormData | bytes | str | None,
    files: FileData | None,
    content: bytes | str | None,
) -> tuple[bytes, str | None]:
    if json is not None:
        return (
            json_module.dumps(json, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            ),
            "application/json; charset=utf-8",
        )
    if files is not None:
        return _encode_multipart(data, files)
    if isinstance(content, bytes):
        return content, None
    if isinstance(content, str):
        return content.encode("utf-8"), None
    if isinstance(data, bytes):
        return data, None
    if isinstance(data, str):
        return data.encode("utf-8"), "text/plain; charset=utf-8"
    if data is not None:
        return (
            urlencode(_expand_pairs(data), doseq=True).encode("utf-8"),
            "application/x-www-form-urlencoded",
        )
    return b"", None


def _expand_pairs(items: QueryParamInput | FormData) -> list[tuple[str, str]]:
    seq = items.items() if isinstance(items, Mapping) else items
    out: list[tuple[str, str]] = []
    for key, value in seq:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for v in value:
                if isinstance(v, bool):
                    sv = "true" if v else "false"
                else:
                    sv = str(v)
                out.append((str(key), sv))
        else:
            v = value
            if isinstance(v, bool):
                sv = "true" if v else "false"
            else:
                sv = str(v)
            out.append((str(key), sv))
    return out


def _encode_multipart(
    data: FormData | bytes | str | None,
    files: FileData,
) -> tuple[bytes, str]:
    if isinstance(data, (bytes, str)):
        raise ValueError("Multipart requests accept mapping or sequence form `data` only.")

    boundary = f"stario-boundary-{uuid7().hex}"
    parts: list[bytes] = []

    if data is not None:
        for name, value in _expand_pairs(data):
            esc = name.replace('"', '\\"')
            disp = f'Content-Disposition: form-data; name="{esc}"'
            parts.extend(
                (
                    f"--{boundary}\r\n".encode("ascii"),
                    disp.encode("utf-8"),
                    b"\r\n\r\n",
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )

    if isinstance(files, Mapping):
        file_items = files.items()
    else:
        file_items = files

    for field_name, file_value in file_items:
        if isinstance(file_value, (bytes, str)):
            filename, payload, content_type = field_name, file_value, "application/octet-stream"
        elif len(file_value) == 2:
            filename, payload = file_value
            content_type = "application/octet-stream"
        else:
            filename, payload, content_type = file_value
        payload_bytes = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        esc = field_name.replace('"', '\\"')
        sf = filename.replace('"', '\\"')
        disp = (
            f'Content-Disposition: form-data; name="{esc}"; '
            f'filename="{sf}"'
        )
        parts.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                disp.encode("utf-8"),
                b"\r\n",
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                payload_bytes,
                b"\r\n",
            )
        )

    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _parse_http_response(raw: bytes) -> tuple[int, Headers, bytes]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise RuntimeError("Malformed test response: missing header separator.")

    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ", 2)
    if len(status_parts) < 2:
        raise RuntimeError("Malformed test response: invalid status line.")
    status_code = int(status_parts[1])
    headers = Headers()
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers.radd(name.lower(), value.lstrip())
    if headers.get("transfer-encoding") == "chunked":
        remaining = body
        decoded = bytearray()
        while remaining:
            size_line, _, rest = remaining.partition(b"\r\n")
            if not rest:
                break
            size = int(size_line.split(b";", 1)[0], 16)
            if size == 0:
                break
            decoded.extend(rest[:size])
            remaining = rest[size + 2 :]
        body = bytes(decoded)
    return status_code, headers, body


def _decode_content_encoding(body: bytes, encoding: str | None) -> bytes:
    if not encoding:
        return body
    normalized = encoding.lower()
    if normalized == "gzip":
        return zlib.decompress(body, wbits=31)
    if normalized == "deflate":
        return zlib.decompress(body)
    if normalized == "br":
        return brotli.decompress(body)
    if normalized == "zstd":
        return zstd.decompress(body)
    return body


def _parse_response_cookies(headers: Headers) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for value in headers.getlist("set-cookie"):
        parsed = http.cookies.SimpleCookie()
        parsed.load(value)
        for name, morsel in parsed.items():
            if morsel.value != "":
                cookies[name] = morsel.value
    return cookies


def _merge_cookie_jar(jar: dict[str, str], headers: Headers) -> None:
    for value in headers.getlist("set-cookie"):
        parsed = http.cookies.SimpleCookie()
        parsed.load(value)
        for name, morsel in parsed.items():
            if morsel["max-age"] == "0" or morsel.value == "":
                jar.pop(name, None)
            else:
                jar[name] = morsel.value


def _serialize_cookie_header(cookies: CookieMap) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _date_header() -> bytes:
    now = datetime.now(timezone.utc)
    return b"date: " + format_datetime(now, usegmt=True).encode("ascii") + b"\r\n"


@asynccontextmanager
async def aload_app(
    bootstrap: BootstrapCandidate,
    *,
    app_factory: Callable[[], App] | None = None,
    tracer: "TestTracer | None" = None,
) -> AsyncIterator[App]:
    """Load ``bootstrap`` like production; uses ``TestTracer`` for startup/shutdown spans.

    Pass ``tracer`` to share one ``TestTracer`` with ``TestClient`` (same instance
    as ``client.tracer`` when the client wires bootstrap).
    """
    app = app_factory() if app_factory is not None else App()
    t = tracer if tracer is not None else TestTracer()
    span = t.create("server.startup")
    span.start()
    span.attr("test.aload_app", True)
    state: Literal["starting", "running"] = "starting"
    shutting_down = False

    def start_shutdown_span(
        trigger: Literal["expected_stop", "runtime_failure", "fallback_cleanup"],
    ) -> None:
        nonlocal shutting_down
        if state == "starting":
            return
        if shutting_down:
            return
        shutdown_span = t.create("server.shutdown")
        shutdown_span.link(span)
        shutdown_span.attr("server.shutdown.trigger", trigger)
        shutdown_span.start()
        span.id = shutdown_span.id
        shutting_down = True

    try:
        async with normalize_bootstrap(bootstrap)(app, span):
            span.end()
            state = "running"
            yield app
            start_shutdown_span("expected_stop")
    except BaseException as exc:
        if state == "running":
            start_shutdown_span("runtime_failure")
        span.exception(exc)
        span.fail(str(exc))
        raise
    finally:
        if state == "starting":
            span.end()
        elif state == "running" and not shutting_down:
            start_shutdown_span("fallback_cleanup")
        if shutting_down:
            span.end()
