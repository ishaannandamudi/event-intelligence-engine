"""Small retained synthetic sources, with independently supplied event clocks."""

from hashlib import sha256

from eie.core.quality import ConfirmationState
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent, StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.interactions.models import InteractionPolicy, InteractionQuery
from eie.interactions.repository import InteractionRepository
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

DEFAULT_POLICY = InteractionPolicy()
MINUTE = 60_000_000_000
DATASET = "synthetic-interactions"
SOURCE = SourceIdentity("INTERACTION_FIXTURE", "1", "1", SourceClass.COMPANY_PRIMARY)


def retained_event(
    repo: InteractionRepository,
    key: str,
    known: int,
    *,
    time: int | None = None,
    entity: str = "iss_fixture",
    scope: ScopeKind = ScopeKind.COMPANY,
    family: EventFamily = EventFamily.EARNINGS,
    code: str = "EARNINGS.RELEASE",
    bundle: str | None = None,
    source_time: int | None = None,
    scheduled: int | None = None,
    date: str | None = None,
    state: str | None = None,
    thread: str | None = None,
    prior: CanonicalEvent | None = None,
    correction: bool = False,
    retracted: bool = False,
    title: str = "Synthetic event",
    dataset: str = DATASET,
) -> CanonicalEvent:
    store = repo.store
    source_time = known if source_time is None else source_time
    source_key = bundle or key + ":" + str(known)
    raw = RawSourceObservation(
        SOURCE,
        source_key,
        "1",
        sha256(source_key.encode()).hexdigest(),
        "application/json",
        source_time,
        source_time,
    )
    store.append_observation(raw)
    facts = {}
    if date is not None:
        facts["announced_date"] = date
    if state is not None:
        facts["interaction_state"] = state
    if thread is not None:
        facts["interaction_thread"] = thread
    number = 1 if prior is None else prior.revision.number + 1
    event = CanonicalEvent(
        EventIdentity(
            EventType(family, code),
            EventScope(
                scope, () if scope in (ScopeKind.MARKET, ScopeKind.MACRO_GLOBAL) else (entity,)
            ),
            key,
        ),
        EventRevision(
            number,
            RevisionKind.ORIGINAL
            if prior is None
            else RevisionKind.CANCELLATION
            if retracted
            else RevisionKind.CORRECTION
            if correction
            else RevisionKind.UPDATE,
            known,
            None if prior is None else number - 1,
        ),
        LifecycleState.CANCELLED
        if retracted
        else LifecycleState.SCHEDULED
        if prior is None and state == "RUMORED"
        else LifecycleState.RELEASED
        if prior is None or prior.lifecycle is LifecycleState.SCHEDULED
        else LifecycleState.UPDATED,
        ConfirmationState.REPORTED,
        (raw.observation_id,),
        frozenset({SOURCE.source_class}),
        title,
        effective_at_ns=time,
        scheduled_for_ns=scheduled,
        facts=tuple(StructuredFact(k, v) for k, v in sorted(facts.items())),
    )
    store.append_event(event)
    repo.index_event(dataset, event.event_id, known)
    return event


def query(
    repo: InteractionRepository,
    event: CanonicalEvent,
    known: int,
    policy: InteractionPolicy = DEFAULT_POLICY,
    **kwargs: object,
) -> InteractionQuery:
    repo.append_policy(policy)
    return InteractionQuery(DATASET, event.event_id, known, policy.artifact_id, **kwargs)
