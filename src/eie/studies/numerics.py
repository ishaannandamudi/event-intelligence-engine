"""Finite stable descriptive reductions."""

from collections.abc import Iterable
from math import isfinite

from eie.core.errors import ValidationError


def median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    if not ordered or any(not isfinite(v) for v in ordered):
        raise ValidationError("median requires samples")
    index = len(ordered) // 2
    return ordered[index] if len(ordered) % 2 else ordered[index - 1] / 2 + ordered[index] / 2
