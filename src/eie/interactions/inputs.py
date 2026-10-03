"""Project retained canonical evidence into a compact, versioned event index."""

from eie.core.errors import ValidationError
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import RevisionKind
from eie.evidence.claims import TimePrecision, TimeValue
from eie.evidence.queries import EvidenceQueries
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimStatus, ClaimType, Predicate
from eie.interactions.models import InteractionEvent
from eie.interactions.schema import DomainState, InteractionQuality
from eie.interactions.windows import exact


def build_event(repo: EvidenceRepository, dataset: str, event: CanonicalEvent) -> InteractionEvent:
    facts = {f.name: f.value for f in event.facts}
    known = event.revision.available_from_ns
    occurrence = exact(
        next(
            (
                t
                for t in (event.effective_at_ns, event.announced_at_ns, event.published_at_ns)
                if t is not None
            ),
            None,
        )
    )
    scheduled = exact(event.scheduled_for_ns)
    refs: set[str] = set()
    # Existing typed coarse schedules take precedence over any synthetic study anchor.
    if facts.get("schedule_precision") in ("DATE_ONLY", "DATE_PLUS_SESSION", "UNKNOWN"):
        scheduled = TimeValue(TimePrecision.UNKNOWN)
        if isinstance((date := facts.get("scheduled_date")), str):
            from eie.corporate.types import ReleaseTiming

            precision = TimePrecision(str(facts["schedule_precision"]))
            session = ReleaseTiming(str(facts.get("release_timing", "UNKNOWN")))
            scheduled = TimeValue(precision, date, session=session)
    if occurrence.precision is TimePrecision.UNKNOWN and isinstance(
        (date := facts.get("announced_date")), str
    ):
        occurrence = TimeValue(TimePrecision.DATE_ONLY, date)
    state: DomainState | LifecycleState = event.lifecycle
    declared = facts.get("interaction_state", facts.get("status"))
    if isinstance(declared, str) and declared in {s.value for s in DomainState}:
        state = DomainState(declared)
    thread = facts.get("interaction_thread", facts.get("corporate_id", event.event_id))
    if not isinstance(thread, str):
        raise ValidationError("structured event thread must have a stable identifier")
    retracted = event.revision.kind is RevisionKind.CANCELLATION
    rows = repo.observations(dataset, known, event_id=event.event_id)
    queries = EvidenceQueries(repo, dataset)
    times: dict[Predicate, set[TimeValue]] = {}
    for claim_id in sorted({r.claim_id for r in rows}):
        claim = repo.claim(claim_id)
        if claim.claim_type in (ClaimType.EVENT_TIME, ClaimType.EARNINGS_SCHEDULE):
            resolved = queries.resolution(claim_id, known)
            if isinstance(resolved.value, TimeValue):
                times.setdefault(claim.predicate, set()).add(resolved.value)
                refs.update(resolved.active_ids)
    for predicate, values in times.items():
        if len(values) != 1:
            raise ValidationError("ambiguous source timing cannot provide interaction time")
        value = next(iter(values))
        if predicate is Predicate.EFFECTIVE_AT:
            occurrence = value
        elif predicate is Predicate.SCHEDULED_FOR:
            scheduled = value
    if rows:
        latest: dict[str, object] = {}
        for r in rows:
            latest[r.thread_id] = r
        retracted |= any(
            r.status is ClaimStatus.RETRACTED for r in rows if latest[r.thread_id] is r
        )
    raw_times = []
    for i in event.evidence_observation_ids:
        raw = repo.store.get_observation(i)
        if raw is None:
            raise ValidationError("interaction event requires retained source observations")
        raw_times.append(max(raw.source_observed_at_ns, raw.ingested_at_ns))
    quality: set[InteractionQuality] = set()
    if occurrence.precision is TimePrecision.UNKNOWN:
        quality.add(InteractionQuality.UNKNOWN_TIME)
    elif occurrence.precision is not TimePrecision.EXACT:
        quality.add(InteractionQuality.COARSE_TIME)
    return InteractionEvent(
        dataset,
        event.event_id,
        event.revision.number,
        event.revision_id,
        event.result_fingerprint,
        event.identity.event_type,
        event.identity.scope,
        occurrence,
        scheduled,
        max(raw_times),
        known,
        event.evidence_observation_ids,
        event.revision.kind,
        thread,
        state,
        retracted,
        frozenset(quality),
        tuple(sorted(refs)),
    )
