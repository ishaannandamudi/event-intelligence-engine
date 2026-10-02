"""Indexed overlap flags from existing event history, without attribution or NLP."""

from bisect import bisect_left, bisect_right

from eie.events.models import CanonicalEvent
from eie.events.scope import ScopeKind
from eie.events.taxonomy import EventFamily
from eie.studies.models import ContaminationFlag


def event_time(event: CanonicalEvent) -> tuple[int, str] | None:
    for t, basis in (
        (event.published_at_ns, "PUBLICATION"),
        (event.effective_at_ns, "EFFECTIVE"),
        (event.scheduled_for_ns, "SCHEDULED"),
    ):
        if t is not None:
            return t, basis
    return None


class ContaminationIndex:
    def __init__(self, events: tuple[CanonicalEvent, ...]) -> None:
        rows = [(t[0], e.event_id, t[1], e) for e in events if (t := event_time(e)) is not None]
        self.rows = tuple(sorted(rows, key=lambda r: (r[0], r[1])))
        self.times = tuple(r[0] for r in self.rows)
        self.unknown_count = len(events) - len(rows)

    def flags(
        self, event: CanonicalEvent, anchor: int, start: int, end: int
    ) -> tuple[ContaminationFlag, ...]:
        flags = []
        for t, _, basis, other in self.rows[
            bisect_left(self.times, start) : bisect_right(self.times, end)
        ]:
            if other.event_id == event.event_id:
                continue
            reasons = set()
            if other.identity.scope.kind is event.identity.scope.kind and set(
                other.identity.scope.identifiers
            ) & set(event.identity.scope.identifiers):
                reasons.add("SAME_ENTITY_EVENT")
            if other.identity.event_type.family in (EventFamily.MACRO, EventFamily.CENTRAL_BANK):
                reasons.add("MACRO_OVERLAP")
            if other.identity.scope.kind in (ScopeKind.MARKET, ScopeKind.MACRO_GLOBAL):
                reasons.add("MARKET_WIDE_OVERLAP")
            if t == anchor:
                reasons.add("CO_OCCURRENCE")
            if set(other.evidence_observation_ids) & set(event.evidence_observation_ids):
                reasons.add("SOURCE_BUNDLE")
            if other.scheduled_for_ns is not None:
                reasons.add("SCHEDULED_OVERLAP")
            if reasons:
                flags.append(
                    ContaminationFlag(
                        other.event_id,
                        other.revision_id,
                        t,
                        basis,
                        tuple(sorted(reasons)),
                        other.revision.available_from_ns,
                    )
                )
        return tuple(flags)
