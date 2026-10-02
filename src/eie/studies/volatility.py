"""Unannualized sqrt(sum(log return squared)), without bridging missing intervals."""

from math import hypot, isfinite

from eie.studies.alignment import active_duration
from eie.studies.market import MarketBar, MarketSession
from eie.studies.models import ReturnMethod, VolatilityResponse
from eie.studies.returns import raw_return


def realized(
    bars: tuple[MarketBar, ...], sessions: tuple[MarketSession, ...], minimum: int
) -> tuple[float | None, int]:
    values = []
    for a, b in zip(bars, bars[1:], strict=False):
        if a.end_ns != b.start_ns and (
            not sessions or active_duration(a.end_ns, b.start_ns, sessions) > 0
        ):
            continue
        v = raw_return(a.close, b.close, ReturnMethod.LOG)
        if v is not None:
            values.append(v)
    rv = hypot(*values) if len(values) >= minimum else None
    return rv, len(values)


def volatility(
    pre: tuple[MarketBar, ...],
    post: tuple[MarketBar, ...],
    sessions: tuple[MarketSession, ...],
    minimum: int,
) -> VolatilityResponse:
    before, n = realized(pre, sessions, minimum)
    after, m = realized(post, sessions, minimum)
    difference = None if before is None or after is None else after - before
    ratio = None if before is None or after is None or before == 0 else after / before
    if ratio is not None and not isfinite(ratio):
        ratio = None
    return VolatilityResponse(before, after, difference, ratio, n, m)
