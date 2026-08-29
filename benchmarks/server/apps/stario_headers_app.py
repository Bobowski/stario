"""Cython-only request-header access workloads.

These routes keep the response constant and validate every lookup so the wrk
comparison measures request-header representation rather than silent misses.
"""

from stario import App, Span

BODY = b"ok"
CONTENT_TYPE = b"text/plain"


def no_reads(_c, w):
    w.respond(BODY, CONTENT_TYPE)


def one_read(c, w):
    if c.req.headers.get("authorization") != "Bearer benchmark-token":
        w.respond(b"bad authorization", CONTENT_TYPE, 500)
        return
    w.respond(BODY, CONTENT_TYPE)


def three_reads(c, w):
    headers = c.req.headers
    values = (
        headers.get("authorization"),
        headers.get("user-agent"),
        headers.get("x-request-id"),
    )
    if values != (
        "Bearer benchmark-token",
        "wrk-header-benchmark",
        "request-1",
    ):
        w.respond(b"bad headers", CONTENT_TYPE, 500)
        return
    w.respond(BODY, CONTENT_TYPE)


def cookie_list(c, w):
    if c.req.headers.getlist("cookie") != ["session=abc123; theme=dark"]:
        w.respond(b"bad cookie", CONTENT_TYPE, 500)
        return
    w.respond(BODY, CONTENT_TYPE)


def response_headers(_c, w):
    w.headers.set("cache-control", "no-store")
    w.headers.set("x-request-id", "request-1")
    w.headers.add("vary", "origin")
    w.headers.add("vary", "accept-language")
    w.respond(BODY, CONTENT_TYPE)


async def bootstrap(app: App, _span: Span) -> None:
    app.get("/headers/none", no_reads)
    app.get("/headers/one", one_read)
    app.get("/headers/three", three_reads)
    app.get("/headers/cookies", cookie_list)
    app.get("/headers/response", response_headers)
    yield
