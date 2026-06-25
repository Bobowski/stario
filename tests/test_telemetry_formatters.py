"""Tests for shared telemetry formatting helpers."""

from decimal import Decimal
from uuid import UUID

import pytest

from stario.exceptions import StarioError
from stario.telemetry.formatters import (
    dumps_json,
    format_exception_for_telemetry,
    serialize_event_body,
)


class TestFormatExceptionForTelemetry:
    def test_exception_without_traceback_formats_message_only(self):
        text = format_exception_for_telemetry(ValueError("plain"))
        assert text == "ValueError: plain\n"
        assert "Traceback" not in text

    def test_exception_with_traceback_includes_application_frames(self):
        def app_level() -> None:
            raise RuntimeError("inner boom")

        with pytest.raises(RuntimeError) as exc_info:
            app_level()
        text = format_exception_for_telemetry(exc_info.value)

        assert "Traceback (most recent call last):" in text
        assert "app_level" in text
        assert text.endswith("RuntimeError: inner boom\n")

    def test_frames_inside_stario_are_hidden(self):
        # Raise through a stario-internal helper: its frames must not leak
        # into the formatted traceback, only the application frames.
        from stario.markup import render

        def app_caller() -> None:
            render(object())  # type: ignore[arg-type]

        with pytest.raises(StarioError, match="Cannot render element of type object") as exc_info:
            app_caller()
        text = format_exception_for_telemetry(exc_info.value)

        assert "app_caller" in text
        assert "/stario/html/" not in text.replace("\\", "/")

    def test_exception_raised_purely_inside_stario_falls_back_to_message(self):
        from stario.exceptions import StarioError

        # No application frame at all (synthesized): formatter degrades to
        # exception-only text rather than an empty traceback.
        exc = StarioError("internal only")
        assert "internal only" in format_exception_for_telemetry(exc)


class TestSerializeEventBody:
    def test_string_passes_through(self):
        assert serialize_event_body("hello") == "hello"

    def test_other_objects_are_rejected(self):
        with pytest.raises(TypeError, match="structured data in attributes"):
            serialize_event_body({"a": 1})  # type: ignore[arg-type]


class TestJsonHelpers:
    def test_dumps_json_is_compact_and_unicode(self):
        assert dumps_json({"a": 1, "msg": "日本語"}) == '{"a":1,"msg":"日本語"}'

    def test_dumps_json_falls_back_to_str_for_unknown_types(self):
        uid = UUID("00000000-0000-0000-0000-000000000001")
        assert dumps_json({"id": uid}) == f'{{"id":"{uid}"}}'
        assert dumps_json({"d": Decimal("1.5")}) == '{"d":"1.5"}'
