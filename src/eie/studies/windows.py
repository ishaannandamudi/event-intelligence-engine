"""Explicit elapsed or supplied-session clocks; no invented calendar or event times."""

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.events.models import CanonicalEvent
from eie.studies.market import MarketSession
from eie.studies.models import (
    AnchorQuality,
    AnchorSemantics,
    CoarsePolicy,
    EventAnchor,
    EventWindow,
    SessionPolicy,
)


def event_anchor(
    event: CanonicalEvent,
    semantics: AnchorSemantics,
    *,
    accepted_ns: int | None = None,
    eie_available_ns: int | None = None,
) -> EventAnchor:
    if semantics is AnchorSemantics.SCHEDULED and any(
        f.name == "schedule_precision" and f.value in ("DATE_ONLY", "DATE_PLUS_SESSION", "UNKNOWN")
        for f in event.facts
    ):
        raise ValidationError("coarse source schedule cannot provide exact anchor")
    values = {
        AnchorSemantics.PUBLICATION: event.published_at_ns,
        AnchorSemantics.SCHEDULED: event.scheduled_for_ns,
        AnchorSemantics.SOURCE_AVAILABILITY: event.revision.available_from_ns,
        AnchorSemantics.ACCEPTANCE: accepted_ns,
        AnchorSemantics.EIE_AVAILABILITY: eie_available_ns,
    }
    t = values[semantics]
    if t is None:
        raise ValidationError(
            "chosen event timestamp is unavailable; coarse policy requires explicit anchor"
        )
    return EventAnchor(semantics, AnchorQuality.EXACT_TIMESTAMP, t)


def resolve_anchor(
    anchor: EventAnchor, window: EventWindow, sessions: tuple[MarketSession, ...]
) -> int:
    if anchor.quality is AnchorQuality.EXACT_TIMESTAMP:
        assert anchor.timestamp_ns is not None
        return anchor.timestamp_ns
    if anchor.quality is AnchorQuality.UNKNOWN or window.coarse_policy is CoarsePolicy.REJECT:
        raise ValidationError("exact study rejects coarse/unknown event time")
    matches = [s for s in sessions if s.label == anchor.session_label]
    if len(matches) != 1:
        raise ValidationError("coarse study requires a unique supplied session")
    s = matches[0]
    t = s.open_ns if window.coarse_policy is CoarsePolicy.SESSION_OPEN else s.close_ns
    if s.available_ns > t:
        raise ValidationError("future session metadata cannot resolve anchor")
    return t


def offset_time(
    anchor: int, offset: int, policy: SessionPolicy, sessions: tuple[MarketSession, ...]
) -> int:
    if policy is SessionPolicy.ELAPSED or offset == 0:
        return require_ns(anchor + offset)
    remaining, cursor = abs(offset), anchor
    ordered = sessions if offset > 0 else tuple(reversed(sessions))
    for s in ordered:
        if s.available_ns > anchor:
            continue
        if offset > 0 and s.close_ns > cursor:
            start = max(cursor, s.open_ns)
            length = s.close_ns - start
            if remaining <= length:
                return start + remaining
            remaining -= length
            cursor = s.close_ns
        elif offset < 0 and s.open_ns < cursor:
            end = min(cursor, s.close_ns)
            length = end - s.open_ns
            if remaining <= length:
                return end - remaining
            remaining -= length
            cursor = s.open_ns
    raise ValidationError("supplied sessions do not cover trading-time window")
