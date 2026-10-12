"""Tests for catch_errors middleware and framework-mapped exceptions."""

import asyncio
import logging

from stario import App, Route
from stario.exceptions import RedirectException, RequestBodyError
from stario.http.context import Context
from stario.http.middleware import catch_errors
from stario.http.writer import Writer
from stario.testing import TestClient
from tests.helpers import run_with_app


class TestCatchErrors:
    def test_maps_listed_exception_when_nothing_sent(self):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, exc: BaseException) -> None:
            w.text(str(exc), 422)

        async def handler(_c: Context, _w: Writer) -> None:
            raise AppError("nope")

        def setup(app) -> None:
            app.use("/", catch_errors(AppError, respond=respond))
            app.add(Route("GET /x"), handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 422
        assert writer.body == "nope"
        assert writer.completed

    def test_re_raises_after_response_started(self, caplog):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, _exc: BaseException) -> None:
            w.text("handled", 422)

        async def handler(_c: Context, w: Writer) -> None:
            w.respond(b"partial", b"text/plain", 200)
            raise AppError("after write")

        async def run() -> None:
            app = App()
            app.use("/", catch_errors(AppError, respond=respond))
            app.add(Route("GET /x"), handler)
            async with TestClient(app) as client:
                response = await client.get("/x")
            assert response.status_code == 200
            assert response.text == "partial"

        with caplog.at_level(logging.ERROR, logger="stario.http"):
            asyncio.run(run())

        assert "Handler failed" in caplog.text


class TestFrameworkExceptions:
    """The protocol answers RedirectException/RequestBodyError itself."""

    def test_request_body_error_maps_to_its_status(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RequestBodyError(413, "too big")

        async def run() -> None:
            app = App()
            app.add(Route("POST /upload"), handler)
            async with TestClient(app) as client:
                response = await client.post("/upload")
            assert response.status_code == 413
            assert response.text == "too big"
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.ok is True

        asyncio.run(run())

    def test_redirect_exception_maps_to_its_location(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RedirectException(301, "/new")

        async def run() -> None:
            app = App()
            app.add(Route("GET /old"), handler)
            async with TestClient(app) as client:
                response = await client.get("/old", follow_redirects=False)
            assert response.status_code == 301
            assert response.headers.get("location") == "/new"
            span = client.tracer.get_span(response.span_id)
            assert span is not None
            assert span.ok is True

        asyncio.run(run())

    def test_redirect_exception_after_response_started_keeps_it(self, caplog):
        async def handler(_c: Context, w: Writer) -> None:
            w.text("kept")
            raise RedirectException(301, "/new")

        async def run() -> None:
            app = App()
            app.add(Route("GET /old"), handler)
            async with TestClient(app) as client:
                response = await client.get("/old", follow_redirects=False)
            assert response.status_code == 200
            assert response.text == "kept"

        with caplog.at_level(logging.ERROR, logger="stario.http"):
            asyncio.run(run())

        assert "Handler failed" in caplog.text
