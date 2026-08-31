"""Python HTTP/1.1 protocol using zttp (Zig pull parser) instead of httptools.

Selected with ``STARIO_HTTP_PARSER=zttp``. The Cython server does not use this:
complete requests go through ``vendor/stario_h1`` and the rest through llhttp.
This module exists so the Python path can A/B zttp against httptools.
"""

from __future__ import annotations

from typing import Any, cast

import zttp

from stario.exceptions import StarioError

from .protocol import HttpProtocol as HttpToolsProtocol


class _ParserShim:
    """Stand-in for ``httptools.HttpRequestParser`` getters."""

    __slots__ = ("_keep_alive", "_method", "_version")

    def __init__(self) -> None:
        self._method = b"GET"
        self._version = "1.1"
        self._keep_alive = True

    def get_method(self) -> bytes:
        return self._method

    def get_http_version(self) -> str:
        return self._version

    def should_keep_alive(self) -> bool:
        return self._keep_alive


class HttpProtocol(HttpToolsProtocol):
    """Same connection/pipeline/timeout behavior; zttp pulls events."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.parser = _ParserShim()
        self._zttp = zttp.Connection(zttp.SERVER)
        self._shim = cast(_ParserShim, self.parser)

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        self._zttp = None

    def data_received(self, data: bytes) -> None:
        if self._rejected:
            return
        conn = self._zttp
        if conn is None:
            return
        if self._timeout_kind == "idle":
            self._cancel_timeout()
        try:
            event = conn.receive_event(data)
            self._dispatch_event(event)
            self._drain_events()
        except zttp.RemoteProtocolError:
            self._close_with_error(400, "Invalid HTTP request")
        except zttp.LocalProtocolError as exc:
            raise StarioError(
                "zttp local protocol error",
                context={"error": str(exc)},
            ) from exc

    def _drain_events(self) -> None:
        conn = self._zttp
        if conn is None or self._rejected:
            return
        while True:
            event = conn.next_event()
            if event is zttp.NEED_DATA:
                return
            self._dispatch_event(event)
            if self._rejected:
                return

    def _dispatch_event(self, event: object) -> None:
        if event is zttp.NEED_DATA:
            return
        if isinstance(event, zttp.Request):
            self._on_zttp_request(event)
            return
        if isinstance(event, zttp.Data):
            self.on_body(event.data)
            return
        if isinstance(event, zttp.EndOfMessage):
            self._finish_zttp_message()

    def _on_zttp_request(self, event: zttp.Request) -> None:
        conn = self._zttp
        if conn is None:
            return
        self.on_message_begin()
        self.on_url(event.target)
        for name, value in event.headers:
            self.on_header(name, value)
        self._shim._method = event.method
        version = event.http_version
        self._shim._version = (
            version.decode("ascii") if isinstance(version, bytes) else str(version)
        )
        self._shim._keep_alive = not conn.should_close()
        self.parser = self._shim
        self.on_headers_complete()
        if event.end_stream:
            self._finish_zttp_message()

    def _finish_zttp_message(self) -> None:
        self.on_message_complete()
        conn = self._zttp
        if conn is None or self._rejected:
            return
        conn.start_next_cycle()
        self._drain_events()
