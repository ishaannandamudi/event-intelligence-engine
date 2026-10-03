"""Typed labels and large text are validated separately; text is never an instruction."""

from dataclasses import fields

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.studies.checks import hints, matches


def checked(value: object, *, text_fields: frozenset[str] = frozenset()) -> None:
    for f in fields(value):  # type: ignore[arg-type]
        item = getattr(value, f.name)
        if not matches(item, hints(type(value))[f.name]):
            raise ValidationError(f"invalid typed extraction field: {f.name}")
        if f.name.endswith("_ns") and isinstance(item, int):
            require_ns(item, f.name)
        if (
            isinstance(item, str)
            and f.name not in text_fields
            and (
                not item
                or item != item.strip()
                or len(item) > 256
                or any(ord(c) < 32 for c in item)
            )
        ):
            raise ValidationError("bounded extraction label required")


def bounded(value: int, maximum: int, *, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError("explicit extraction bound required")
