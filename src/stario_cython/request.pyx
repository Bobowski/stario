# cython: language_level=3
"""Request view. Same handler API as stario.http.request.Request."""

from types import MappingProxyType

from stario import cookies as cookie_helpers
from stario.exceptions import HttpException
from stario.http.query import ParsedQuery

from stario_cython.headers cimport Headers


cdef class Request:
    def __init__(
        self,
        *,
        method="GET",
        path="/",
        query_bytes=b"",
        protocol_version="1.1",
        keep_alive=True,
        headers=None,
        body=None,
    ):
        self.reset(
            method, path, query_bytes, protocol_version, keep_alive, headers, body
        )

    cdef void reset(
        self,
        object method,
        object path,
        object query_bytes,
        object protocol_version,
        bint keep_alive,
        object headers,
        object body,
    ):
        self.method = method
        self.path = path
        self.headers = headers
        self.protocol_version = protocol_version
        self.keep_alive = keep_alive
        self.query_bytes = query_bytes
        self._body = body
        self._query = None
        self._cookies = None
        self._host = None

    @property
    def host(self):
        cdef object host_str
        cdef object host
        cdef object host_wire
        cdef object rest
        cdef object host_part
        cdef object port_part
        cdef Py_ssize_t bracket_end
        if self._host is not None:
            return self._host
        if isinstance(self.headers, Headers):
            host_wire = (<Headers>self.headers).c_request_host()
            host_str = (
                host_wire.decode("latin-1").strip()
                if host_wire is not None
                else ""
            )
        else:
            host_str = (self.headers.get("host") or "").strip()
        if not host_str:
            self._host = ""
        elif host_str.startswith("["):
            bracket_end = host_str.find("]")
            if bracket_end == -1:
                self._host = host_str.lower()
            else:
                host = host_str[: bracket_end + 1].lower()
                rest = host_str[bracket_end + 1 :]
                if rest and (not rest.startswith(":") or not rest[1:].isdigit()):
                    self._host = host_str.lower()
                else:
                    self._host = host
        elif ":" in host_str:
            host_part, _, port_part = host_str.rpartition(":")
            self._host = (
                host_part.lower()
                if port_part.isdigit() and host_part
                else host_str.lower()
            )
        else:
            self._host = host_str.lower()
        return self._host

    @property
    def query(self):
        if self._query is None:
            self._query = ParsedQuery(self.query_bytes)
        return self._query

    @property
    def cookies(self):
        if self._cookies is None:
            self._cookies = cookie_helpers.parse_cookie_headers(
                self.headers.getlist("cookie")
            )
        return MappingProxyType(self._cookies)

    async def body(self, max_size=None):
        if max_size is not None and max_size < 0:
            raise ValueError("max_size must be non-negative.")
        if self._body is None:
            return b""
        if type(self._body) is bytes:
            if max_size is not None and len(self._body) > max_size:
                raise HttpException(413, "Request body too large")
            return self._body
        return await self._body.read(max_size=max_size)

    async def stream(self, max_chunk=None):
        if self._body is None:
            return
        if type(self._body) is bytes:
            yield self._body
            return
        async for chunk in self._body.stream(max_chunk=max_chunk):
            yield chunk
