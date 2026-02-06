"""
Testing - ResponseRecorder for unit tests.

No server needed. Call handlers directly.

Usage:
    from stario.testing import ResponseRecorder, TestRequest

    async def test_users():
        w = ResponseRecorder()
        r = TestRequest(path="/users")

        await get_users(db)(w, r)

        assert w.status_code == 200
        assert w.json_body() == [...]
"""

import asyncio
import http.cookies
import json as json_module
from contextlib import contextmanager
from datetime import datetime
from email.utils import format_datetime
from typing import Any, AsyncIterator, Iterator, Literal
from urllib.parse import urlencode

from stario.html import HtmlElement
from stario.html import render as html_render
from stario.http.headers import Headers
from stario.http.request import Request


def TestRequest(
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: dict[str, Any] | None = None,
) -> Request:
    """Create a Request for testing."""
    from stario.http.request import BodyReader

    hdrs = Headers()
    if headers:
        for k, v in headers.items():
            hdrs.set(k, v)

    # Create body reader (required by Request)
    # Don't create a Future - it requires an event loop
    reader = BodyReader(
        pause=lambda: None,
        resume=lambda: None,
        disconnect=None,
    )
    reader._cached = body
    reader._complete = True

    req = Request(
        method=method,
        path=path,
        headers=hdrs,
        body=reader,
    )

    if query:
        req._query_bytes = urlencode(query, doseq=True).encode()

    return req


class ResponseRecorder:
    """
    Fake Writer for testing.

    Records all output for assertions.
    Matches Writer API for Datastar methods.
    """

    __slots__ = (
        "status_code",
        "headers",
        "body",
        "sse_events",
        "datastar_events",
        "_started",
        "_closed",
        "_disconnect",
        "_mode",
        "_alive",
    )

    def __init__(self) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.body = bytearray()
        self.sse_events: list[dict[str, Any]] = []
        self.datastar_events: list[dict[str, Any]] = []
        self._started = False
        self._closed = False
        self._disconnect = asyncio.Event()
        self._mode: Literal["none", "sse", "oneshot"] = "none"
        self._alive: "_AliveRecorder | None" = None

    # =========================================================================
    # Connection state
    # =========================================================================

    @property
    def disconnected(self) -> bool:
        return self._disconnect.is_set()

    @property
    def disconnect(self) -> asyncio.Event:
        return self._disconnect

    @property
    def alive(self) -> "_AliveRecorder":
        """Async iterator and context manager for testing."""
        if self._alive is None:
            self._alive = _AliveRecorder(self)
        return self._alive

    # =========================================================================
    # Chainable methods
    # =========================================================================

    def status(self, code: int) -> "ResponseRecorder":
        if self._started:
            raise RuntimeError(
                "Cannot set status after response started. "
                "Set status before any write operations. Use: w.status(code).html(...)"
            )
        self.status_code = code
        return self

    def header(self, name: str | bytes, value: str | bytes) -> "ResponseRecorder":
        if self._started:
            raise RuntimeError(
                "Cannot set headers after response started. "
                "Set headers via w.header() before any write operations."
            )
        name_str = name.decode() if isinstance(name, bytes) else name
        value_str = value.decode() if isinstance(value, bytes) else value
        self.headers[name_str.lower()] = value_str
        return self

    def cookie(
        self,
        name: str,
        value: str = "",
        *,
        max_age: int | None = None,
        expires: datetime | str | int | None = None,
        path: str | None = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> "ResponseRecorder":
        cookie: http.cookies.BaseCookie[str] = http.cookies.SimpleCookie()
        cookie[name] = value
        if max_age is not None:
            cookie[name]["max-age"] = max_age
        if expires is not None:
            if isinstance(expires, datetime):
                cookie[name]["expires"] = format_datetime(expires, usegmt=True)
            else:
                cookie[name]["expires"] = expires
        if path:
            cookie[name]["path"] = path
        if domain:
            cookie[name]["domain"] = domain
        if secure:
            cookie[name]["secure"] = True
        if httponly:
            cookie[name]["httponly"] = True
        if samesite:
            cookie[name]["samesite"] = samesite

        cookie_val = cookie.output(header="").strip()
        existing = self.headers.get("set-cookie", "")
        if existing:
            self.headers["set-cookie"] = existing + "; " + cookie_val
        else:
            self.headers["set-cookie"] = cookie_val
        return self

    def delete_cookie(
        self,
        name: str,
        *,
        path: str | None = "/",
        domain: str | None = None,
    ) -> "ResponseRecorder":
        """Delete a cookie by setting it to expire immediately."""
        return self.cookie(
            name,
            "",
            max_age=0,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
            path=path,
            domain=domain,
        )

    # =========================================================================
    # SSE/Datastar streaming (primary mode)
    # =========================================================================

    def _ensure_sse(self) -> None:
        if self._mode == "oneshot":
            raise RuntimeError(
                "Cannot stream after one-shot response started. "
                "You called a one-shot method (html/json/text) before streaming methods (patch/sync). "
                "Use streaming methods only, or one-shot methods only-not both."
            )
        if self._mode == "none":
            self._mode = "sse"
            self.header("content-type", "text/event-stream")
            self.header("cache-control", "no-cache")
            self.header("connection", "keep-alive")

    def patch(
        self,
        element: Any,
        *,
        mode: Literal[
            "outer", "inner", "prepend", "append", "before", "after"
        ] = "outer",
        selector: str | None = None,
        use_view_transition: bool = False,
    ) -> None:
        """Record a patch event."""
        self._ensure_sse()
        self.datastar_events.append(
            {
                "type": "patch",
                "element": element,
                "mode": mode,
                "selector": selector,
            }
        )

    def sync(
        self,
        data: dict[str, Any],
        *,
        only_if_missing: bool = False,
    ) -> None:
        """Record a sync (signals update) event."""
        self._ensure_sse()
        self.datastar_events.append(
            {
                "type": "sync",
                "data": data,
                "only_if_missing": only_if_missing,
            }
        )

    def navigate(self, url: str) -> None:
        """Record a navigate event (SSE redirect)."""
        self._ensure_sse()
        self.datastar_events.append({"type": "navigate", "url": url})

    def script(self, code: str, *, auto_remove: bool = True) -> None:
        """Record a script event."""
        self._ensure_sse()
        self.datastar_events.append(
            {
                "type": "script",
                "code": code,
                "auto_remove": auto_remove,
            }
        )

    def remove(self, selector: str) -> None:
        """Record a remove event."""
        self._ensure_sse()
        self.datastar_events.append({"type": "remove", "selector": selector})

    def retry(self, milliseconds: int) -> None:
        """Record retry directive."""
        self._ensure_sse()
        self.sse_events.append({"type": "retry", "data": milliseconds})

    def comment(self, text: str = "") -> None:
        """Record a comment."""
        self._ensure_sse()
        self.sse_events.append({"type": "comment", "data": text})

    # =========================================================================
    # One-shot responses
    # =========================================================================

    def respond(
        self,
        body: bytes,
        content_type: bytes | str,
        status: int = 200,
    ) -> None:
        """Send whole response."""
        ct = content_type.decode() if isinstance(content_type, bytes) else content_type
        self.headers.setdefault("content-type", ct)
        self._send_complete(body, status, None)

    def json(
        self,
        data: Any,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json_module.dumps(
            data, separators=(",", ":"), ensure_ascii=False
        ).encode()
        self.headers.setdefault("content-type", "application/json; charset=utf-8")
        self._send_complete(body, status, headers)

    def html(
        self,
        content: HtmlElement,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Send HTML response. Matches Writer.html() signature."""
        body = html_render(content).encode("utf-8")
        self.headers.setdefault("content-type", "text/html; charset=utf-8")
        self._send_complete(body, status, headers)

    def text(
        self,
        content: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        body = content.encode("utf-8")
        self.headers.setdefault("content-type", content_type)
        self._send_complete(body, status, headers)

    def redirect(
        self,
        url: str,
        status: Literal[301, 302, 303, 307, 308] = 307,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.headers["location"] = url
        self._send_complete(b"", status, headers)

    def empty(
        self,
        status: Literal[204, 304] = 204,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._send_complete(b"", status, headers)

    def _send_complete(
        self,
        body: bytes,
        status: int,
        headers: dict[str, str] | None,
    ) -> None:
        if self._mode == "sse":
            raise RuntimeError(
                "Cannot send one-shot response after SSE streaming started. "
                "You called patch()/sync() before html()/json()/text(). "
                "Use streaming methods only, or one-shot methods only-not both."
            )
        if self._started:
            raise RuntimeError(
                "Response already started. Each handler should only send one response. "
                "Check for multiple calls to html()/json()/text()/redirect()."
            )
        self._mode = "oneshot"
        self.status_code = status
        if headers:
            for k, v in headers.items():
                self.headers[k.lower()] = v
        self.body.extend(body)
        self._started = True
        self._closed = True

    # =========================================================================
    # Streaming
    # =========================================================================

    def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError(
                "Response already closed. Cannot write after close() or after one-shot response."
            )
        self._started = True
        self.body.extend(data)

    def close(self) -> None:
        if not self._started:
            self._started = True
        self._closed = True

    # =========================================================================
    # Legacy SSE context
    # =========================================================================

    @contextmanager
    def sse(self) -> Iterator["SSERecorder"]:
        """SSE context for testing (legacy)."""
        self.header("content-type", "text/event-stream")
        self.header("cache-control", "no-cache")
        self.header("connection", "keep-alive")
        self._mode = "sse"
        yield SSERecorder(self)
        self.close()

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    # =========================================================================
    # Assertions
    # =========================================================================

    def json_body(self) -> Any:
        """Parse body as JSON."""
        return json_module.loads(self.body)

    def text_body(self) -> str:
        """Get body as string."""
        return self.body.decode("utf-8")

    def assert_status(self, expected: int) -> None:
        assert (
            self.status_code == expected
        ), f"Expected {expected}, got {self.status_code}"

    def assert_json(self, expected: Any) -> None:
        actual = self.json_body()
        assert actual == expected, f"Expected {expected}, got {actual}"

    def assert_header(self, name: str, expected: str) -> None:
        actual = self.headers.get(name.lower())
        assert actual == expected, f"Expected {name}={expected}, got {actual}"


class _AliveRecorder:
    """Testing version of _Alive."""

    __slots__ = ("_w", "_source")

    def __init__(self, w: ResponseRecorder, source: Any = None):
        self._w = w
        self._source = source

    def __call__(self, source: Any) -> "_AliveRecorder":
        return _AliveRecorder(self._w, source)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        async with self:
            if self._source is None:
                while True:
                    if self._w.disconnected:
                        return
                    yield None
            else:
                async for item in self._source:
                    if self._w.disconnected:
                        return
                    yield item

    async def __aenter__(self) -> "_AliveRecorder":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_type is asyncio.CancelledError:
            if self._w.disconnected:
                return True
        return False


class SSERecorder:
    """SSE writer for testing."""

    __slots__ = ("_w",)

    def __init__(self, w: ResponseRecorder) -> None:
        self._w = w

    @property
    def disconnected(self) -> bool:
        return self._w.disconnected

    @property
    def disconnect(self) -> asyncio.Event:
        return self._w.disconnect

    def send(self, data: Any, *, id: str | None = None) -> None:
        self._w.sse_events.append({"type": "message", "data": data, "id": id})
        if isinstance(data, (dict, list)):
            data = json_module.dumps(data, separators=(",", ":"), ensure_ascii=False)
        lines = []
        if id is not None:
            lines.append(f"id: {id}\n")
        for line in str(data).split("\n"):
            lines.append(f"data: {line}\n")
        lines.append("\n")
        self._w.write("".join(lines).encode())

    def event(self, name: str, data: Any, *, id: str | None = None) -> None:
        self._w.sse_events.append({"type": name, "data": data, "id": id})
        if isinstance(data, (dict, list)):
            data = json_module.dumps(data, separators=(",", ":"), ensure_ascii=False)
        lines = [f"event: {name}\n"]
        if id is not None:
            lines.append(f"id: {id}\n")
        for line in str(data).split("\n"):
            lines.append(f"data: {line}\n")
        lines.append("\n")
        self._w.write("".join(lines).encode())

    def comment(self, text: str = "") -> None:
        self._w.sse_events.append({"type": "comment", "data": text})
        self._w.write(f": {text}\n\n".encode())

    def retry(self, milliseconds: int) -> None:
        self._w.sse_events.append({"type": "retry", "data": milliseconds})
        self._w.write(f"retry: {milliseconds}\n\n".encode())
