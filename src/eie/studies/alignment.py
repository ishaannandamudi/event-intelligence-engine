"""Completed-bar alignment with explicit tolerance and supplied session coverage."""

from dataclasses import dataclass

from eie.studies.market import MarketBar, MarketSession, MarketSlice
from eie.studies.models import (
    AlignmentPolicy,
    ReferencePolicy,
    SessionPolicy,
    StudyPolicy,
    StudyQuality,
)


def active_duration(start: int, end: int, sessions: tuple[MarketSession, ...]) -> int:
    return sum(max(0, min(end, s.close_ns) - max(start, s.open_ns)) for s in sessions)


def lag(start: int, end: int, mode: SessionPolicy, sessions: tuple[MarketSession, ...]) -> int:
    return end - start if mode is SessionPolicy.ELAPSED else active_duration(start, end, sessions)


def point_bar(
    bars: tuple[MarketBar, ...],
    target: int,
    anchor: int,
    end: int,
    policy: StudyPolicy,
    mode: SessionPolicy,
    sessions: tuple[MarketSession, ...],
) -> MarketBar | None:
    if policy.alignment is AlignmentPolicy.LAST_COMPLETED:
        eligible = [
            b for b in bars if b.end_ns <= target and (target <= anchor or b.start_ns >= anchor)
        ]
        b = eligible[-1] if eligible else None
        distance = None if b is None else lag(b.end_ns, target, mode, sessions)
    else:
        eligible = [
            b
            for b in bars
            if b.start_ns >= target and b.end_ns <= (anchor if target < anchor else end)
        ]
        b = eligible[0] if eligible else None
        distance = None if b is None else lag(target, b.start_ns, mode, sessions)
    if (
        b is None
        or distance is None
        or distance > policy.max_lag_ns
        or distance >= b.timeframe.duration_ns
    ):
        return None
    return b


@dataclass(frozen=True, slots=True)
class AlignedWindow:
    pre: tuple[MarketBar, ...]
    post: tuple[MarketBar, ...]
    reference_price: float | None
    reference_ns: int | None
    quality: frozenset[StudyQuality]


def align(
    data: MarketSlice, start: int, anchor: int, end: int, policy: StudyPolicy, mode: SessionPolicy
) -> AlignedWindow:
    pre = tuple(
        b
        for b in data.bars
        if b.start_ns >= start and b.end_ns <= anchor and b.available_ns <= anchor
    )
    post = tuple(b for b in data.bars if b.start_ns >= anchor and b.end_ns <= end)
    quality: set[StudyQuality] = set()
    if not pre:
        quality.add(StudyQuality.MISSING_PRE_DATA)
    if not post:
        quality.add(StudyQuality.MISSING_POST_DATA)
    sessions = tuple(s for s in data.sessions if s.available_ns <= anchor)
    if sessions and not any(s.open_ns <= anchor < s.close_ns for s in sessions):
        quality.add(StudyQuality.MARKET_CLOSED)
    if any(b.start_ns < anchor < b.end_ns for b in data.bars):
        quality.add(StudyQuality.PARTIAL_SESSION)
    if any(
        b.end_ns - b.start_ns != b.timeframe.duration_ns and b.timeframe.value != "daily"
        for b in (*pre, *post)
    ):
        quality.add(StudyQuality.PARTIAL_SESSION)
    ranges = (
        ((start, end),)
        if not sessions
        else tuple(
            (max(start, s.open_ns), min(end, s.close_ns))
            for s in sessions
            if s.close_ns > start and s.open_ns < end
        )
    )
    for left, right in ranges:
        cursor = left
        for b in (*pre, *post):
            if b.end_ns <= left or b.start_ns >= right:
                continue
            if b.start_ns > cursor:
                quality.add(StudyQuality.DATA_GAP)
            cursor = max(cursor, b.end_ns)
        if cursor < right:
            quality.add(StudyQuality.DATA_GAP)
    if data.gaps:
        quality.add(StudyQuality.DATA_GAP)
    reference = None
    reference_ns = None
    if policy.reference is ReferencePolicy.PRE_CLOSE and pre:
        b = pre[-1]
        distance = lag(b.end_ns, anchor, mode, sessions)
        if distance <= policy.max_lag_ns and distance < b.timeframe.duration_ns:
            reference, reference_ns = b.close, b.end_ns
    elif policy.reference is ReferencePolicy.FIRST_POST_OPEN and post:
        b = post[0]
        distance = lag(anchor, b.start_ns, mode, sessions)
        if distance <= policy.max_lag_ns and distance < b.timeframe.duration_ns:
            reference, reference_ns = b.open, b.start_ns
    elif policy.reference is ReferencePolicy.ALIGNED_CLOSE and post:
        b = post[0]
        distance = lag(anchor, b.start_ns, mode, sessions)
        if distance <= policy.max_lag_ns and distance < b.timeframe.duration_ns:
            reference, reference_ns = b.close, b.end_ns
    return AlignedWindow(pre, post, reference, reference_ns, frozenset(quality))
