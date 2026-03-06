"""Datastar signals parsing using cached pydantic adapters."""

from functools import lru_cache
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from stario.exceptions import SignalValidationError


@lru_cache(maxsize=256)
def _adapter_for(schema: Any) -> TypeAdapter[Any]:
    """Build and cache a pydantic adapter per schema type."""
    return TypeAdapter(schema)


def parse_signals[T](raw: str | bytes, schema: type[T] = dict[str, Any]) -> T:
    """
    Parse Datastar signals.

    - schema default is dict[str, Any]
    - schema is provided: validate using pydantic TypeAdapter (cached by type)
    - all validation failures raise SignalValidationError
    """
    try:
        return _adapter_for(schema).validate_json(raw)
    except ValidationError as exc:
        raise SignalValidationError.from_validation_error(exc) from exc


class FileSignal(BaseModel):
    """Datastar file upload signal payload."""

    name: str
    contents: str
    mime: str | None = None
