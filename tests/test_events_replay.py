from dataclasses import replace
from datetime import UTC, datetime

import pytest

from eie.core.errors import (
    InvalidLifecycleTransition,
    PersistenceError,
    ReplayError,
    SchemaCompatibilityError,
    TemporalConsistencyError,
    ValidationError,
)
from eie.core.quality import ConfirmationState, QualityFlag
from eie.core.serialization import canonical_json_bytes
from eie.core.time import datetime_to_ns
from eie.events.identity import EventIdentity
from eie.events.lifecycle import EventWindowState, LifecycleState, validate_transition
from eie.events.models import CanonicalEvent, StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import ApplicabilityQuery, EventScope, ScopeKind, applies_directly
from eie.events.taxonomy import MACRO_CPI_US, REPRESENTATIVE_TYPES, EventFamily, EventType
from eie.persistence.memory import InMemoryEventStore
from eie.replay.clock import ReplayClock
from eie.replay.models import KnownEvents
from eie.replay.protocols import StoreReplayReader
from eie.sources.models import RawSourceObservation
from eie.sources.trust import SourceClass


def test_taxonomy_scope_and_identity() -> None:
    assert len(REPRESENTATIVE_TYPES) >= 40
    assert EventType(EventFamily.PRODUCT_BUSINESS, "PRODUCT.NEW_THING") == EventType(
        EventFamily.PRODUCT_BUSINESS, "PRODUCT.NEW_THING"
    )
    with pytest.raises(ValidationError):
        EventType(EventFamily.MACRO, "PRODUCT.WRONG")
    with pytest.raises(ValidationError):
        EventType(EventFamily.MACRO, "macro.bad")
    with pytest.raises(ValidationError):
        EventType("MACRO", "MACRO.CPI.US")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        EventIdentity(MACRO_CPI_US, EventScope(ScopeKind.MARKET), "")
    with pytest.raises(ValidationError):
        EventScope(ScopeKind.COMPANY)
    with pytest.raises(ValidationError):
        EventScope(ScopeKind.MACRO_GLOBAL, ("US",))
    with pytest.raises(ValidationError):
        EventScope(ScopeKind.COMPANY, ("z", "a"))
    with pytest.raises(ValidationError):
        EventScope(ScopeKind.COMPANY, ("",))
    query = ApplicabilityQuery(
        symbol="ABC",
        security_id="sec_1",
        issuer_id="iss_1",
        industry="software",
        sector="tech",
        index="SP500",
        market="US",
        country="US",
    )
    for kind, identifier in (
        (ScopeKind.SYMBOL, "ABC"),
        (ScopeKind.SECURITY, "sec_1"),
        (ScopeKind.COMPANY, "iss_1"),
        (ScopeKind.INDUSTRY, "software"),
        (ScopeKind.SECTOR, "tech"),
        (ScopeKind.INDEX, "SP500"),
        (ScopeKind.MACRO_COUNTRY, "US"),
    ):
        assert applies_directly(EventScope(kind, (identifier,)), query)
        assert not applies_directly(EventScope(kind, ("different",)), query)
    assert applies_directly(EventScope(ScopeKind.MARKET), query)
    assert applies_directly(EventScope(ScopeKind.MACRO_GLOBAL), ApplicabilityQuery())
    assert not applies_directly(EventScope(ScopeKind.MARKET), ApplicabilityQuery())


def test_lifecycle_and_revision_validation(event: CanonicalEvent) -> None:
    validate_transition(LifecycleState.SCHEDULED, LifecycleState.RELEASED)
    validate_transition(LifecycleState.RELEASED, LifecycleState.UPDATED)
    assert EventWindowState.PRE_EVENT != LifecycleState.SCHEDULED
    with pytest.raises(InvalidLifecycleTransition):
        validate_transition(LifecycleState.CANCELLED, LifecycleState.RELEASED)
    with pytest.raises(InvalidLifecycleTransition):
        validate_transition(LifecycleState.SCHEDULED, LifecycleState.UPDATED)
    with pytest.raises(ValidationError):
        EventRevision(1, RevisionKind.UPDATE, 1)
    with pytest.raises(ValidationError):
        EventRevision(2, RevisionKind.ORIGINAL, 2, 1)
    with pytest.raises(ValidationError):
        EventRevision(2, RevisionKind.CORRECTION, 2, 0)
    with pytest.raises(ValidationError):
        EventRevision(2, RevisionKind.CORRECTION, 2, 1, reason_codes=("z", "a"))
    with pytest.raises(ValidationError):
        EventRevision(2, RevisionKind.CORRECTION, 2, 1, supersedes_event_id="bad")
    with pytest.raises(ValidationError):
        EventRevision(0, RevisionKind.ORIGINAL, 1)
    with pytest.raises(ValidationError):
        EventRevision(2, RevisionKind.CORRECTION, 2, 1, reason_codes=("",))
    with pytest.raises(ValidationError):
        EventRevision(1, "ORIGINAL", 1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        EventIdentity(MACRO_CPI_US, "bad", "key")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        EventScope("MARKET")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        EventRevision(2, RevisionKind.CORRECTION, 2, 1.0)  # type: ignore[arg-type]
    assert (
        EventRevision(
            2, RevisionKind.SUPERSEDED, 2, 1, supersedes_event_id=event.event_id
        ).supersedes_event_id
        == event.event_id
    )
    assert event.revision_id.startswith("rev_")
    assert event.event_id == replace(event, scheduled_for_ns=300).event_id
    assert (
        event.event_payload_fingerprint
        != replace(event, scheduled_for_ns=300).event_payload_fingerprint
    )
    assert event.result_fingerprint == replace(event).result_fingerprint


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"schema_version": "2.0"}, SchemaCompatibilityError),
        ({"title": ""}, ValidationError),
        ({"evidence_observation_ids": ()}, ValidationError),
        ({"evidence_observation_ids": ("bad",)}, ValidationError),
        ({"source_classes": frozenset()}, ValidationError),
        ({"published_at_ns": 104}, TemporalConsistencyError),
        ({"scheduled_for_ns": -1}, TemporalConsistencyError),
        ({"facts": (StructuredFact("z", 1), StructuredFact("a", 1))}, ValidationError),
        ({"facts": (StructuredFact("a", 1), StructuredFact("a", 2))}, ValidationError),
        ({"lifecycle": LifecycleState.CANCELLED}, ValidationError),
        ({"lifecycle": LifecycleState.RESCHEDULED}, ValidationError),
    ],
)
def test_event_validation(
    event: CanonicalEvent, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(event, **changes)


def test_facts_confirmation_quality() -> None:
    assert len(ConfirmationState) == 6
    assert QualityFlag.CORRECTED in QualityFlag
    assert StructuredFact("actual", 2.1, "%").value == 2.1
    with pytest.raises(ValidationError):
        StructuredFact("x", float("nan"))
    with pytest.raises(ValidationError):
        StructuredFact("x", float("inf"))
    with pytest.raises(ValidationError):
        StructuredFact("", 1)
    with pytest.raises(ValidationError):
        StructuredFact("x", 1, "")
    with pytest.raises(ValidationError):
        StructuredFact("x", [1])  # type: ignore[arg-type]


def test_typed_event_fields_and_timestamp_order(event: CanonicalEvent) -> None:
    with pytest.raises(ValidationError):
        replace(event, evidence_observation_ids=["obs_bad"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        replace(event, source_classes=frozenset({"OFFICIAL_PRIMARY"}))  # type: ignore[arg-type]
    with pytest.raises(TemporalConsistencyError):
        replace(event, published_at_ns=102, source_observed_at_ns=100)
    with pytest.raises(TemporalConsistencyError):
        replace(event, source_observed_at_ns=102, ingested_at_ns=101)
    with pytest.raises(ValidationError):
        replace(event, revision=EventRevision(2, RevisionKind.CANCELLATION, 110, 1))
    with pytest.raises(ValidationError):
        replace(event, revision=EventRevision(2, RevisionKind.RESCHEDULE, 110, 1))


def test_causal_revisions_reschedule_and_cancellation(
    raw: RawSourceObservation, event: CanonicalEvent
) -> None:
    store = InMemoryEventStore()
    assert store.append_observation(raw)
    assert not store.append_observation(raw)
    assert store.get_observation(raw.observation_id) == raw
    assert store.append_event(event)
    assert not store.append_event(event)
    assert store.get_event(event.event_id, 102) is None
    assert store.get_event(event.event_id, 103) == event
    rescheduled = replace(
        event,
        revision=EventRevision(2, RevisionKind.RESCHEDULE, 110, 1),
        lifecycle=LifecycleState.RESCHEDULED,
        scheduled_for_ns=300,
    )
    store.append_event(rescheduled)
    back_to_scheduled = replace(
        event, revision=EventRevision(3, RevisionKind.REVISION, 120, 2), scheduled_for_ns=300
    )
    store.append_event(back_to_scheduled)
    cancelled = replace(
        event,
        revision=EventRevision(4, RevisionKind.CANCELLATION, 130, 3),
        lifecycle=LifecycleState.CANCELLED,
        scheduled_for_ns=300,
    )
    store.append_event(cancelled)
    reader = StoreReplayReader(store)
    assert reader.events_known_at(102).events == ()
    assert reader.events_known_at(109).events == (event,)
    assert reader.events_known_at(110).events == (rescheduled,)
    assert reader.events_known_at(119).events == (rescheduled,)
    assert reader.events_known_at(120).events == (back_to_scheduled,)
    assert reader.events_known_at(130).events == (cancelled,)
    assert store.get_revisions(event.event_id) == (event, rescheduled, back_to_scheduled, cancelled)
    assert store.known_as_of(109, start_ns=200, end_ns=201) == (event,)
    assert store.known_as_of(110, start_ns=200, end_ns=201) == ()
    assert store.known_as_of(110, start_ns=300, end_ns=301) == (rescheduled,)
    with pytest.raises(InvalidLifecycleTransition):
        store.append_event(
            replace(
                event,
                revision=EventRevision(5, RevisionKind.REVISION, 140, 4),
                lifecycle=LifecycleState.RELEASED,
            )
        )
    with pytest.raises(TemporalConsistencyError):
        store.known_as_of(130, start_ns=301, end_ns=300)


def test_release_correction_visibility_and_scope(
    raw: RawSourceObservation, event: CanonicalEvent
) -> None:
    store = InMemoryEventStore()
    store.append_observation(raw)
    released = replace(
        event,
        lifecycle=LifecycleState.RELEASED,
        revision=EventRevision(1, RevisionKind.ORIGINAL, 103),
    )
    store.append_event(released)
    corrected = replace(
        released,
        revision=EventRevision(
            2, RevisionKind.CORRECTION, 110, 1, reason_codes=("SOURCE_CORRECTION",)
        ),
        lifecycle=LifecycleState.UPDATED,
        facts=(StructuredFact("actual", 2.1, "%"),),
        quality_flags=frozenset({QualityFlag.CORRECTED}),
    )
    store.append_event(corrected)
    assert store.get_event(event.event_id, 109) == released
    assert store.get_event(event.event_id, 110) == corrected
    assert store.known_as_of(110, ApplicabilityQuery(country="US")) == (corrected,)
    assert store.known_as_of(110, ApplicabilityQuery(country="CA")) == ()
    assert store.known_as_of(110, start_ns=200, end_ns=201) == (corrected,)
    assert store.canonical_state_bytes() == store.canonical_state_bytes()


def test_store_rejects_conflicts_and_future_evidence(
    raw: RawSourceObservation, event: CanonicalEvent
) -> None:
    store = InMemoryEventStore()
    with pytest.raises(PersistenceError):
        store.append_event(event)
    store.append_observation(raw)
    with pytest.raises(PersistenceError):
        store.append_observation(replace(raw, ingested_at_ns=102))
    with pytest.raises(TemporalConsistencyError):
        store.append_event(
            replace(
                event, revision=EventRevision(1, RevisionKind.ORIGINAL, 100), ingested_at_ns=None
            )
        )
    with pytest.raises(PersistenceError):
        store.append_event(replace(event, source_classes=frozenset({SourceClass.UNVERIFIED})))
    with pytest.raises(PersistenceError):
        store.append_event(replace(event, revision=EventRevision(2, RevisionKind.REVISION, 120, 1)))
    store.append_event(event)
    with pytest.raises(PersistenceError):
        store.append_event(replace(event, title="changed"))
    with pytest.raises(TemporalConsistencyError):
        store.append_event(
            replace(
                event,
                revision=EventRevision(2, RevisionKind.REVISION, 103, 1),
                lifecycle=LifecycleState.CONFIRMED,
            )
        )
    with pytest.raises(PersistenceError):
        store.append_event(
            replace(
                event,
                revision=EventRevision(2, RevisionKind.RESCHEDULE, 110, 1),
                lifecycle=LifecycleState.RESCHEDULED,
            )
        )
    with pytest.raises(InvalidLifecycleTransition):
        store.append_event(
            replace(
                event,
                revision=EventRevision(2, RevisionKind.UPDATE, 110, 1),
                lifecycle=LifecycleState.UPDATED,
            )
        )
    no_time = replace(
        event,
        identity=EventIdentity(
            MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), "other-period"
        ),
        scheduled_for_ns=None,
    )
    store.append_event(no_time)
    assert store.known_as_of(103, start_ns=1) == (event,)


def test_named_release_and_revision_boundaries(
    raw: RawSourceObservation, event: CanonicalEvent
) -> None:
    def at(hour: int, minute: int) -> int:
        return datetime_to_ns(datetime(2026, 9, 1, hour, minute, tzinfo=UTC))

    publication = at(14, 3)
    correction = at(14, 10)
    observed = replace(
        raw,
        source_observed_at_ns=publication,
        ingested_at_ns=publication,
        published_at_ns=publication,
    )
    released = replace(
        event,
        revision=EventRevision(1, RevisionKind.ORIGINAL, publication),
        lifecycle=LifecycleState.RELEASED,
        evidence_observation_ids=(observed.observation_id,),
        published_at_ns=publication,
        source_observed_at_ns=publication,
        ingested_at_ns=publication,
    )
    corrected = replace(
        released,
        revision=EventRevision(2, RevisionKind.CORRECTION, correction, 1),
        lifecycle=LifecycleState.UPDATED,
        facts=(StructuredFact("actual", 2.1, "%"),),
    )
    store = InMemoryEventStore()
    store.append_observation(observed)
    store.append_event(released)
    store.append_event(corrected)
    assert store.known_as_of(at(14, 2)) == ()
    assert store.known_as_of(at(14, 9)) == (released,)
    assert store.known_as_of(at(14, 10)) == (corrected,)


def test_cross_event_supersession(raw: RawSourceObservation, event: CanonicalEvent) -> None:
    store = InMemoryEventStore()
    store.append_observation(raw)
    store.append_event(event)
    successor = replace(
        event,
        identity=EventIdentity(MACRO_CPI_US, event.identity.scope, "replacement-period"),
        revision=EventRevision(1, RevisionKind.ORIGINAL, 110, supersedes_event_id=event.event_id),
    )
    assert store.append_event(successor)
    assert successor.event_id != event.event_id
    with pytest.raises(PersistenceError):
        store.append_event(
            replace(
                successor,
                revision=EventRevision(
                    2, RevisionKind.REVISION, 120, 1, supersedes_event_id=successor.event_id
                ),
            )
        )
    with pytest.raises(PersistenceError):
        store.append_event(
            replace(
                successor,
                identity=EventIdentity(MACRO_CPI_US, event.identity.scope, "unknown-target"),
                revision=EventRevision(
                    1, RevisionKind.ORIGINAL, 110, supersedes_event_id="evt_" + "0" * 64
                ),
            )
        )


def test_clock_known_events_and_repeated_run(
    raw: RawSourceObservation, event: CanonicalEvent
) -> None:
    def run() -> bytes:
        store = InMemoryEventStore()
        store.append_observation(raw)
        store.append_event(event)
        return canonical_json_bytes(StoreReplayReader(store).events_known_at(103))

    assert run() == run()
    clock = ReplayClock(102)
    store = InMemoryEventStore()
    store.append_observation(raw)
    store.append_event(event)
    reader = StoreReplayReader(store)
    assert reader.at_clock(clock).events == ()
    clock.advance(1)
    assert reader.at_clock(clock).events == (event,)
    clock.set(0)
    assert reader.at_clock(clock).events == ()
    with pytest.raises(ReplayError):
        clock.advance(-1)
    with pytest.raises(TemporalConsistencyError):
        clock.set(-1)
    with pytest.raises(ValueError):
        KnownEvents(102, (event,))
