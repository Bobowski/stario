"""Tests for stario.datastar.parse - pydantic based signals parsing."""

import json
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict

import pytest
from pydantic import BaseModel, ValidationError

from stario.datastar.parse import _adapter_for, parse_signals
from stario.exceptions import SignalValidationError


def _raw(data: dict[str, Any]) -> str:
    return json.dumps(data)


class TestParseSignalsNoSchema:
    def test_returns_dict_for_valid_json_object(self):
        result = parse_signals(_raw({"name": "test", "count": 42}))
        assert result == {"name": "test", "count": 42}

    def test_missing_payload_returns_empty_dict(self):
        with pytest.raises(SignalValidationError) as exc:
            parse_signals(None)
        assert exc.value.errors(include_url=False)[0]["type"] == "json_type"

    def test_invalid_json_raises_signal_validation_error(self):
        with pytest.raises(SignalValidationError) as exc:
            parse_signals("{invalid")
        assert exc.value.errors(include_url=False)[0]["type"] == "json_invalid"

    def test_non_object_json_raises_signal_validation_error(self):
        with pytest.raises(SignalValidationError) as exc:
            parse_signals(json.dumps([1, 2, 3]))
        assert exc.value.errors(include_url=False)[0]["type"] == "dict_type"


class TestParseSignalsDataclassAndTypedDict:
    def test_dataclass_parsing_and_coercion(self):
        @dataclass
        class FormData:
            count: int
            active: bool = False

        result = parse_signals(_raw({"count": "42", "active": "true"}), FormData)
        assert isinstance(result, FormData)
        assert result.count == 42
        assert result.active is True

    def test_typeddict_parsing_and_coercion(self):
        class FormData(TypedDict):
            count: int
            nickname: NotRequired[str]

        result = parse_signals(_raw({"count": "42", "nickname": "ada"}), FormData)
        assert result["count"] == 42
        assert result["nickname"] == "ada"

    def test_nested_dataclass_and_typeddict(self):
        class ProfileDict(TypedDict):
            age: int
            nickname: NotRequired[str]

        @dataclass
        class Payload:
            profile: ProfileDict

        result = parse_signals(
            _raw({"profile": {"age": "41", "nickname": "ada"}}),
            Payload,
        )
        assert result.profile["age"] == 41
        assert result.profile["nickname"] == "ada"

    def test_nested_validation_error_has_path(self):
        @dataclass
        class Profile:
            age: int

        @dataclass
        class Payload:
            profile: Profile

        with pytest.raises(SignalValidationError) as exc:
            parse_signals(_raw({"profile": {"age": "not-int"}}), Payload)

        first = exc.value.errors(include_url=False)[0]
        assert first["loc"] == ("profile", "age")


class TestParseSignalsPydantic:
    def test_pydantic_model_parse(self):
        class Payload(BaseModel):
            count: int

        result = parse_signals(_raw({"count": "42"}), Payload)
        assert result.count == 42

    def test_pydantic_validation_error_passthrough(self):
        class Payload(BaseModel):
            count: int

        with pytest.raises(SignalValidationError):
            parse_signals(_raw({"count": "not-int"}), Payload)

    def test_signal_validation_error_is_still_validation_error(self):
        class Payload(BaseModel):
            count: int

        with pytest.raises(ValidationError):
            parse_signals(_raw({"count": "not-int"}), Payload)


class TestAdapterCaching:
    def test_adapter_is_cached_by_schema_type(self):
        @dataclass
        class Payload:
            count: int

        assert _adapter_for(Payload) is _adapter_for(Payload)
