"""Cython protocol for Stario: uvloop owns the socket; llhttp + nghttp2 parse."""

from stario.http.bootstrap import Bootstrap

def run(bootstrap: Bootstrap) -> None: ...
async def serve(
    bootstrap: Bootstrap,
    host: str = "127.0.0.1",
    port: int = 8000,
    backlog: int = 2048,
) -> None: ...
