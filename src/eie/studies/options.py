"""Causal supplied options snapshots; matched expiry/tenor/bucket, no surface model."""

from math import isfinite

from eie.studies.market import OptionsMarketObservation
from eie.studies.models import OptionsResponse


def change(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    v = b - a
    return v if isfinite(v) else None


def options_response(
    values: tuple[OptionsMarketObservation, ...],
    key: tuple[str, str, str] | None,
    start: int,
    anchor: int,
    end: int,
    max_lag: int,
) -> OptionsResponse:
    matches = [v for v in values if (v.expiry, v.tenor, v.bucket) == key]
    pre = [
        v
        for v in matches
        if start <= v.timestamp_ns <= anchor
        and v.available_ns <= anchor
        and anchor - v.timestamp_ns <= max_lag
    ]
    post = [
        v for v in matches if anchor < v.timestamp_ns <= end and end - v.timestamp_ns <= max_lag
    ]
    a, b = (pre[-1] if pre else None), (post[-1] if post else None)
    return OptionsResponse(
        key,
        None if a is None else a.timestamp_ns,
        None if b is None else b.timestamp_ns,
        None if a is None or b is None else change(a.iv, b.iv),
        None if a is None or b is None else change(a.skew, b.skew),
        None if a is None or b is None else change(a.volume, b.volume),
        None if a is None or b is None else change(a.open_interest, b.open_interest),
    )
