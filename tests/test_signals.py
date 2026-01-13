"""Tests for stario.datastar.parse - Signal parsing."""

from dataclasses import dataclass
from typing import NotRequired, TypedDict

import pytest

from stario.datastar.parse import _coerce, parse_signals
from stario.exceptions import StarioError


class TestCoerce:
    """Test type coercion."""

    def test_string_to_int(self):
        assert _coerce("42", int) == 42

    def test_string_to_float(self):
        assert _coerce("3.14", float) == 3.14

    def test_string_true_to_bool(self):
        assert _coerce("true", bool) is True

    def test_string_false_to_bool(self):
        assert _coerce("false", bool) is False

    def test_string_1_to_bool(self):
        assert _coerce("1", bool) is True

    def test_string_0_to_bool(self):
        assert _coerce("0", bool) is False

    def test_int_to_string(self):
        assert _coerce(42, str) == "42"

    def test_none_returns_none(self):
        assert _coerce(None, int) is None

    def test_already_correct_type(self):
        assert _coerce(42, int) == 42
        assert _coerce("hello", str) == "hello"
        assert _coerce(True, bool) is True


class TestParseSignalsDataclass:
    """Test parsing signals into dataclasses."""

    def test_simple_dataclass(self):
        @dataclass
        class FormData:
            name: str = ""
            count: int = 0

        result = parse_signals({"name": "test", "count": 42}, FormData)

        assert isinstance(result, FormData)
        assert result.name == "test"
        assert result.count == 42

    def test_coercion(self):
        @dataclass
        class Data:
            count: int = 0
            active: bool = False

        result = parse_signals({"count": "42", "active": "true"}, Data)

        assert result.count == 42
        assert result.active is True

    def test_missing_uses_default(self):
        @dataclass
        class Data:
            name: str = "default"
            count: int = 100

        result = parse_signals({}, Data)

        assert result.name == "default"
        assert result.count == 100

    def test_extra_fields_ignored(self):
        @dataclass
        class Data:
            name: str = ""

        result = parse_signals({"name": "test", "extra": "ignored"}, Data)

        assert result.name == "test"
        assert not hasattr(result, "extra")


class TestParseSignalsTypedDict:
    """Test parsing signals into TypedDicts."""

    def test_simple_typeddict(self):
        class FormData(TypedDict):
            name: str
            count: int

        result = parse_signals({"name": "test", "count": 42}, FormData)

        assert result["name"] == "test"
        assert result["count"] == 42

    def test_optional_fields(self):
        class Data(TypedDict):
            required: str
            optional: NotRequired[str]

        result = parse_signals({"required": "yes"}, Data)

        assert result["required"] == "yes"
        assert "optional" not in result

    def test_coercion(self):
        class Data(TypedDict):
            count: int

        result = parse_signals({"count": "42"}, Data)

        assert result["count"] == 42


class TestParseSignalsUnsupported:
    """Test error handling for unsupported types."""

    def test_plain_class_raises(self):
        class PlainClass:
            pass

        with pytest.raises(StarioError, match="Unsupported schema type"):
            parse_signals({}, PlainClass)

    def test_primitive_raises(self):
        with pytest.raises(StarioError, match="Unsupported schema type"):
            parse_signals({}, int)
