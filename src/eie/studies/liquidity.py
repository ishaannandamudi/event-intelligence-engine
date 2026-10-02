"""Descriptive volume and quote responses; baseline evidence strictly predates anchor."""

from math import isfinite

from eie.studies.market import MarketBar, MarketQuote, MarketSession
from eie.studies.models import LiquidityResponse, ReturnMethod, StudyPolicy, VolumePoint
from eie.studies.numerics import median
from eie.studies.returns import raw_return


def bucket(bar: MarketBar, sessions: tuple[MarketSession, ...]) -> tuple[str, int] | None:
    if bar.timeframe.value == "daily":
        return "DAILY", 0
    for s in sessions:
        if s.open_ns <= bar.start_ns < s.close_ns:
            return "SESSION", bar.start_ns - s.open_ns
    return None  # no UTC-time-of-day stand-in for a missing exchange calendar


def volume_response(
    pre: tuple[MarketBar, ...],
    post: tuple[MarketBar, ...],
    history: tuple[MarketBar, ...],
    sessions: tuple[MarketSession, ...],
    anchor: int,
    policy: StudyPolicy,
) -> tuple[VolumePoint, ...]:
    result = []
    cumulative = 0.0
    for b in (*pre, *post):
        key = bucket(b, sessions)
        samples = [
            v.volume
            for v in history
            if key is not None
            and v.end_ns < anchor
            and v.available_ns < anchor
            and v.start_ns < b.start_ns
            and anchor - policy.volume_baseline_ns <= v.start_ns
            and bucket(v, sessions) == key
        ]
        baseline = median(samples) if len(samples) >= policy.minimum_baseline_bars else None
        relative = None if baseline is None or baseline == 0 else b.volume / baseline
        if relative is not None and not isfinite(relative):
            relative = None
        cumulative += b.volume
        if not isfinite(cumulative):
            return ()
        result.append(
            VolumePoint(b.start_ns, b.volume, cumulative, baseline, relative, len(samples))
        )
    return tuple(result)


def quote_response(
    quotes: tuple[MarketQuote, ...], start: int, anchor: int, end: int, max_lag: int
) -> LiquidityResponse:
    pre = [
        q
        for q in quotes
        if start <= q.timestamp_ns <= anchor
        and q.available_ns <= anchor
        and anchor - q.timestamp_ns <= max_lag
    ]
    post = [q for q in quotes if anchor < q.timestamp_ns <= end and end - q.timestamp_ns <= max_lag]
    a, b = (pre[-1] if pre else None), (post[-1] if post else None)
    spread = None if b is None else b.ask - b.bid
    midpoint = None if b is None else b.ask / 2 + b.bid / 2
    relative = None if spread is None or midpoint is None or midpoint <= 0 else spread / midpoint
    depth = None if b is None else b.bid_size + b.ask_size
    if depth is not None and not isfinite(depth):
        depth = None
    return LiquidityResponse(
        None if a is None else a.timestamp_ns,
        None if b is None else b.timestamp_ns,
        None if a is None else a.ask - a.bid,
        spread,
        relative,
        depth,
        raw_return(None if a is None else a.ask / 2 + a.bid / 2, midpoint, ReturnMethod.SIMPLE),
    )
