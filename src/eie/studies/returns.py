"""Finite raw returns; log difference avoids overflowing a price ratio."""

from math import isfinite, log

from eie.studies.models import ReturnMethod


def raw_return(reference: float | None, price: float | None, method: ReturnMethod) -> float | None:
    if (
        reference is None
        or price is None
        or reference <= 0
        or price < 0
        or (method is ReturnMethod.LOG and price <= 0)
    ):
        return None
    value = log(price) - log(reference) if method is ReturnMethod.LOG else price / reference - 1
    return value if isfinite(value) else None
