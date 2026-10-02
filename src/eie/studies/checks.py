"""Shared fail-closed validation for immutable study contracts."""

import re
import types
from dataclasses import fields
from math import isfinite
from typing import get_args, get_origin, get_type_hints

from eie.core.errors import ValidationError
from eie.core.time import require_ns

_HINTS: dict[type[object], dict[str, object]] = {}


def hints(cls: type[object]) -> dict[str, object]:
    if cls not in _HINTS:
        _HINTS[cls] = get_type_hints(cls)
    return _HINTS[cls]


def matches(value: object, hint: object) -> bool:
    origin, args = get_origin(hint), get_args(hint)
    if origin is types.UnionType:
        return any(matches(value, arg) for arg in args)
    if origin in (tuple, frozenset):
        if not isinstance(value, origin):
            return False
        if origin is frozenset or (len(args) == 2 and args[1] is Ellipsis):
            return all(matches(v, args[0]) for v in value)
        return len(value) == len(args) and all(
            matches(v, h) for v, h in zip(value, args, strict=True)
        )
    if hint is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(hint, type) and isinstance(value, hint)


def contract(value: object) -> None:
    for field in fields(value):  # type: ignore[arg-type]
        item = getattr(value, field.name)
        if not matches(item, hints(type(value))[field.name]):
            raise ValidationError(f"invalid typed study field: {field.name}")
        if field.name.endswith("_ns") and field.name != "offset_ns" and isinstance(item, int):
            require_ns(item, field.name)
        if isinstance(item, str) and (
            not item or item != item.strip() or len(item) > 256 or any(ord(c) < 32 for c in item)
        ):
            raise ValidationError("bounded nonempty study label required")


def digest(value: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValidationError("study SHA-256 fingerprint required")


def positive(value: int, *, zero: bool = False) -> None:
    if value < int(not zero):
        raise ValidationError("positive study count/duration required")
