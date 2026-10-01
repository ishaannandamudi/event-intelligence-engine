"""Canonical UTF-8 JSON; no implicit object repr or non-finite numbers."""

import json
from dataclasses import fields, is_dataclass
from enum import Enum
from math import isfinite
from typing import TypeAlias

from eie.core.errors import ValidationError

JSONValue: TypeAlias = "None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]"


def json_value(value: object) -> JSONValue:
    if isinstance(value, Enum):
        return json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValidationError("non-finite number")
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValidationError("JSON object keys must be strings")
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [json_value(item) for item in value]
        return sorted(items, key=lambda item: canonical_json_bytes(item))
    raise ValidationError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
