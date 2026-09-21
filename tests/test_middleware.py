"""Tests for catch_errors middleware."""

import logging

import stario.responses as responses
from stario.exceptions import RequestBodyError
from stario.http.context import Context, Handler
from stario.http.middleware import (
    catch_errors,
    catch_request_body_errors,
    respond_request_body_error,
)
from stario.http.writer import Writer
from tests.helpers import run_with_app


class TestCatchErrors:
    def test_maps_listed_exception_when_nothing_sent(self):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, exc: BaseException) -> None:
            responses.text(w, str(exc), 422)

        async def handler(_c: Context, _w: Writer) -> None:
            raise AppError("nope")

        def setup(app) -> None:
            app.use("/", catch_errors(AppError, respond=respond))
            app.get("/x", handler)

        _context, writer = run_with_app(setup, "/x")

        assert writer.status == 422
        assert writer.body == "nope"
        assert writer.completed

    def test_re_raises_after_response_started(self, caplog):
        class AppError(Exception):
            pass

        async def respond(_c: Context, w: Writer, _exc: BaseException) -> None:
            responses.text(w, "handled", 422)

        async def handler(_c: Context, w: Writer) -> None:
            w.respond(b"partial", b"text/plain", 200)
            raise AppError("after write")

        def setup(app) -> None:
            app.use("/", catch_errors(AppError, respond=respond))
            app.get("/x", handler)

        with caplog.at_level(logging.ERROR, logger="stario.http"):
            _context, writer = run_with_app(setup, "/x")

        assert writer.status == 200
        assert writer.body == "partial"
        assert writer.completed
        assert "Handler failed" in caplog.text

    def test_request_body_error_preset(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RequestBodyError(413, "too big")

        def setup(app) -> None:
            app.use("/", catch_request_body_errors())
            app.post("/upload", handler)

        _context, writer = run_with_app(setup, "/upload", method="POST")

        assert writer.status == 413
        assert writer.body == "too big"

    def test_respond_request_body_error_writes_status(self):
        async def handler(_c: Context, _w: Writer) -> None:
            raise RequestBodyError(408, "slow")

        def setup(app) -> None:
            app.use("/", catch_errors(RequestBodyError, respond=respond_request_body_error))
            app.post("/upload", handler)

        _context, writer = run_with_app(setup, "/upload", method="POST")

        assert writer.status == 408
        assert writer.body == "slow"
