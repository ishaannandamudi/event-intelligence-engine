"""Offline causal schedule, window, provenance and coverage tests."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

import pytest

from eie.config.settings import Settings
from eie.core.errors import (
    InvalidLifecycleTransition,
    PersistenceError,
    TemporalConsistencyError,
    ValidationError,
)
from eie.core.quality import ConfirmationState, QualityFlag
from eie.core.schema import SCHEDULE_SCHEMA_VERSION
from eie.core.serialization import canonical_json_bytes
from eie.core.time import MAX_NS, datetime_to_ns
from eie.entities.models import Issuer, Listing, Security, issuer_id, security_id
from eie.entities.registry import InMemoryEntityRegistry
from eie.events.identity import EventIdentity
from eie.events.lifecycle import EventWindowState, LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import ApplicabilityQuery, EventScope, ScopeKind
from eie.events.taxonomy import MACRO_CPI_US, EventFamily, EventType
from eie.replay.clock import ReplayClock
from eie.scheduled.coverage import CoverageRecord
from eie.scheduled.engine import ScheduledEventEngine
from eie.scheduled.index import ScheduledEventIndex
from eie.scheduled.models import (
    ActiveEventWindow,
    ActiveWindowsResult,
    CoverageStatus,
    CoverageSummary,
    NextEventResult,
    QueryOutcome,
    ScheduleClaim,
    ScheduledEventQueryResult,
    ScheduledEventView,
    ScheduleQueryKind,
    ScheduleRevision,
    ScheduleStatus,
)
from eie.scheduled.windows import (
    EventWindowPolicy,
    ScheduledSettings,
    WindowPolicySet,
    window_state,
)
from eie.sources.models import RawSourceObservation, SourceHealth, SourceHealthState, SourceIdentity
from eie.sources.trust import DEFAULT_PRECEDENCE, SourceClass, SourcePrecedencePolicy


def raw_item(
    item: str,
    at_ns: int,
    source_class: SourceClass = SourceClass.OFFICIAL_PRIMARY,
    source_name: str = "official.example",
) -> RawSourceObservation:
    return RawSourceObservation(
        SourceIdentity(source_name, "1", "1", source_class),
        item,
        "1",
        sha256(item.encode()).hexdigest(),
        "application/json",
        at_ns,
        at_ns,
        published_at_ns=at_ns,
    )


def schedule_revision(
    index: ScheduledEventIndex,
    key: str,
    scheduled_for_ns: int,
    available_from_ns: int,
    *,
    number: int = 1,
    kind: RevisionKind = RevisionKind.ORIGINAL,
    lifecycle: LifecycleState = LifecycleState.SCHEDULED,
    event_type: EventType = MACRO_CPI_US,
    scope: EventScope | None = None,
    item: str | None = None,
    proposed_time_ns: int | None = -1,
) -> ScheduleRevision:
    raw = raw_item(item or f"{key}-{number}", available_from_ns)
    index.append_observation(raw)
    selected_time = scheduled_for_ns if proposed_time_ns == -1 else proposed_time_ns
    event = CanonicalEvent(
        EventIdentity(event_type, scope or EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), key),
        EventRevision(number, kind, available_from_ns, number - 1 if number > 1 else None),
        lifecycle,
        ConfirmationState.CONFIRMED_PRIMARY,
        (raw.observation_id,),
        frozenset({SourceClass.OFFICIAL_PRIMARY}),
        key,
        scheduled_for_ns=scheduled_for_ns,
        published_at_ns=available_from_ns,
        source_observed_at_ns=available_from_ns,
        ingested_at_ns=available_from_ns,
    )
    return ScheduleRevision(
        event,
        (
            ScheduleClaim(
                raw.observation_id, SourceClass.OFFICIAL_PRIMARY, selected_time, available_from_ns
            ),
        ),
        raw.observation_id,
    )


def engine_with_policy(
    policy: EventWindowPolicy,
) -> tuple[ScheduledEventIndex, ScheduledEventEngine]:
    settings = Settings(scheduled=ScheduledSettings(windows=WindowPolicySet(default=policy)))
    index = ScheduledEventIndex(settings)
    return index, ScheduledEventEngine(index)


def test_simple_schedule_queries_and_replay_clock() -> None:
    index, engine = engine_with_policy(EventWindowPolicy(10, 5, 7))
    original = schedule_revision(index, "cpi", 100, 10)
    assert index.append(original)
    assert not index.append(original)
    assert original.fingerprint == replace(original).fingerprint
    assert original.claims[0].fingerprint == replace(original.claims[0]).fingerprint
    assert original.event_id == original.event.event_id
    assert original.revision_id == original.event.revision_id
    assert original.status is ScheduleStatus.ACTIVE
    assert engine.resolve_schedule(original.event_id, 9) is None
    assert engine.resolve_schedule(original.event_id, 10) == original
    assert engine.schedule_history(original.event_id) == (original,)
    assert engine.events_known_as_of(9).events == ()
    assert engine.events_known_as_of(10).events[0].revision == original
    upcoming = engine.upcoming(50, ApplicabilityQuery(country="US"), 100)
    assert upcoming.events[0].time_until_ns == 50
    assert upcoming.events[0].scheduled_for_ns == 100
    assert upcoming.outcome is QueryOutcome.MATCHES
    assert engine.next_event(50).event == upcoming.events[0]
    assert engine.next_event(50).event.time_until_ns == 50
    assert engine.events_between(100, 101, 50).events[0].revision == original
    assert engine.events_between(101, 102, 50).events == ()
    assert engine.recent(101, 2).events[0].time_until_ns == -1
    assert engine.recent(100, 2).events == ()
    clock = ReplayClock(9)
    assert engine.upcoming_at_clock(clock).events == ()
    clock.set(50)
    assert engine.upcoming_at_clock(clock).events[0].event_id == original.event_id
    assert engine.active_windows_at_clock(clock).aggregate_state is EventWindowState.NORMAL
    assert index.canonical_state_bytes() == index.canonical_state_bytes()


def test_reschedule_correction_cancellation_and_historical_bytes() -> None:
    index, engine = engine_with_policy(EventWindowPolicy(10, 5, 7))
    original = schedule_revision(index, "meeting", 100, 10)
    index.append(original)
    past_bytes = canonical_json_bytes(engine.upcoming(15, horizon_ns=200))
    moved = schedule_revision(
        index,
        "meeting",
        120,
        20,
        number=2,
        kind=RevisionKind.RESCHEDULE,
        lifecycle=LifecycleState.RESCHEDULED,
    )
    assert moved.event_id == original.event_id
    assert moved.revision_id != original.revision_id
    index.append(moved)
    corrected = schedule_revision(
        index,
        "meeting",
        125,
        25,
        number=3,
        kind=RevisionKind.CORRECTION,
        lifecycle=LifecycleState.SCHEDULED,
    )
    index.append(corrected)
    cancelled = schedule_revision(
        index,
        "meeting",
        125,
        30,
        number=4,
        kind=RevisionKind.CANCELLATION,
        lifecycle=LifecycleState.CANCELLED,
        proposed_time_ns=None,
    )
    index.append(cancelled)
    assert canonical_json_bytes(engine.upcoming(15, horizon_ns=200)) == past_bytes
    assert engine.resolve_schedule(original.event_id, 19) == original
    assert engine.resolve_schedule(original.event_id, 20) == moved
    assert engine.resolve_schedule(original.event_id, 24) == moved
    assert engine.resolve_schedule(original.event_id, 25) == corrected
    assert engine.resolve_schedule(original.event_id, 30) == cancelled
    assert engine.events_between(100, 101, 19).events[0].revision == original
    assert engine.events_between(120, 121, 20).events[0].revision == moved
    assert engine.events_between(125, 126, 25).events[0].revision == corrected
    assert engine.upcoming(29, horizon_ns=200).events[0].scheduled_for_ns == 125
    assert engine.upcoming(30, horizon_ns=200).events == ()
    assert engine.events_known_as_of(30).events[0].revision.status is ScheduleStatus.CANCELLED
    assert (
        engine.events_between(125, 126, 30, include_cancelled=True).events[0].time_until_ns is None
    )
    assert engine.window_state(original.event_id, 30).state is EventWindowState.NORMAL
    assert len(engine.schedule_history(original.event_id)) == 4
    with pytest.raises(InvalidLifecycleTransition):
        index.append(
            schedule_revision(
                index,
                "meeting",
                125,
                40,
                number=5,
                kind=RevisionKind.UPDATE,
                lifecycle=LifecycleState.SCHEDULED,
            )
        )


def test_exact_window_boundaries_and_zero_duration() -> None:
    policy = EventWindowPolicy(10, 5, 7)
    cases = (
        (989, EventWindowState.NORMAL),
        (990, EventWindowState.PRE_EVENT),
        (999, EventWindowState.PRE_EVENT),
        (1000, EventWindowState.EVENT_WINDOW),
        (1004, EventWindowState.EVENT_WINDOW),
        (1005, EventWindowState.POST_EVENT),
        (1011, EventWindowState.POST_EVENT),
        (1012, EventWindowState.NORMAL),
    )
    for stamp, state in cases:
        assert window_state(stamp, 1000, policy) is state
    index, engine = engine_with_policy(policy)
    original = schedule_revision(index, "cpi", 1000, 10)
    index.append(original)
    for stamp, state in cases:
        assert engine.window_state(original.event_id, stamp).state is state
        active = engine.active_windows(stamp)
        assert active.aggregate_state is state
        assert len(active.windows) == int(state is not EventWindowState.NORMAL)
    assert window_state(999, 1000, EventWindowPolicy()) is EventWindowState.NORMAL
    assert window_state(1000, 1000, EventWindowPolicy()) is EventWindowState.NORMAL
    assert window_state(1000, 1000, EventWindowPolicy(0, 0, 5)) is EventWindowState.POST_EVENT
    assert window_state(999, 1000, EventWindowPolicy(5, 0, 0)) is EventWindowState.PRE_EVENT


def test_overlapping_simultaneous_events_and_scope() -> None:
    index, engine = engine_with_policy(EventWindowPolicy(20, 5, 10))
    spec = (
        ("future-a", 110, EventScope(ScopeKind.COMPANY, ("issuer-a",))),
        ("future-b", 110, EventScope(ScopeKind.COMPANY, ("issuer-a",))),
        ("current", 98, EventScope(ScopeKind.COMPANY, ("issuer-a",))),
        ("past", 90, EventScope(ScopeKind.COMPANY, ("issuer-a",))),
        ("other", 98, EventScope(ScopeKind.COMPANY, ("issuer-b",))),
    )
    for key, when, scope in spec:
        index.append(schedule_revision(index, key, when, 10, scope=scope))
    query = ApplicabilityQuery(issuer_id="issuer-a")
    active = engine.active_windows(100, query)
    assert len(active.windows) == 4
    assert {item.state for item in active.windows} == {
        EventWindowState.PRE_EVENT,
        EventWindowState.EVENT_WINDOW,
        EventWindowState.POST_EVENT,
    }
    assert active.aggregate_state is EventWindowState.EVENT_WINDOW
    assert all(
        item.event.revision.event.identity.scope.identifiers == ("issuer-a",)
        for item in active.windows
    )
    simultaneous = engine.events_between(110, 111, 100, query).events
    assert len(simultaneous) == 2
    assert [view.event_id for view in simultaneous] == sorted(
        view.event_id for view in simultaneous
    )
    assert engine.next_event(100, query).event == simultaneous[0]
    assert engine.upcoming(100, ApplicabilityQuery(issuer_id="nobody"), 100).events == ()


def test_direct_scope_and_historical_listing() -> None:
    index, engine = engine_with_policy(EventWindowPolicy())
    query = ApplicabilityQuery(
        symbol="OLD",
        security_id="security-a",
        issuer_id="issuer-a",
        industry="software",
        sector="tech",
        index="SP500",
        market="US",
        country="US",
    )
    scope_specs = (
        (ScopeKind.SYMBOL, ("OLD",)),
        (ScopeKind.SECURITY, ("security-a",)),
        (ScopeKind.COMPANY, ("issuer-a",)),
        (ScopeKind.INDUSTRY, ("software",)),
        (ScopeKind.SECTOR, ("tech",)),
        (ScopeKind.INDEX, ("SP500",)),
        (ScopeKind.MARKET, ()),
        (ScopeKind.MACRO_COUNTRY, ("US",)),
        (ScopeKind.MACRO_GLOBAL, ()),
    )
    for position, (kind, ids) in enumerate(scope_specs):
        index.append(
            schedule_revision(
                index, f"scope-{position}", 100 + position, 10, scope=EventScope(kind, ids)
            )
        )
    assert len(engine.upcoming(50, query, 100).events) == len(scope_specs)
    assert len(engine.upcoming(50, ApplicabilityQuery(country="CA"), 100).events) == 1
    iid = issuer_id("CIK", "0000123456")
    sid = security_id("FIGI", "X")
    registry = InMemoryEntityRegistry()
    registry.add_issuer(Issuer(iid, "Example"))
    registry.add_security(Security(sid, iid, "Common"))
    registry.add_listing(Listing(sid, "OLD", "XNYS", 10, 70))
    registry.add_listing(Listing(sid, "NEW", "XNAS", 70))
    assert registry.resolve("OLD", 50).candidate_ids == (sid,)
    assert registry.resolve("NEW", 50).candidate_ids == ()
    assert registry.resolve("NEW", 80).candidate_ids == (sid,)
    index.append(
        schedule_revision(
            index, "security-event", 120, 10, scope=EventScope(ScopeKind.SECURITY, (sid,))
        )
    )
    old_query = ApplicabilityQuery(
        symbol="OLD", security_id=registry.resolve("OLD", 50).candidate_ids[0]
    )
    assert any(
        view.revision.event.identity.semantic_key == "security-event"
        for view in engine.upcoming(50, old_query, 100).events
    )


def test_source_conflicts_precedence_and_lineage() -> None:
    index, engine = engine_with_policy(EventWindowPolicy())
    official = raw_item("official-time", 10)
    news = raw_item("news-time", 11, SourceClass.SECONDARY_NEWS, "news.example")
    index.append_observation(official)
    index.append_observation(news)
    claims = tuple(
        sorted(
            (
                ScheduleClaim(official.observation_id, SourceClass.OFFICIAL_PRIMARY, 100, 10),
                ScheduleClaim(news.observation_id, SourceClass.SECONDARY_NEWS, 110, 11),
            ),
            key=lambda claim: claim.observation_id,
        )
    )
    event = CanonicalEvent(
        EventIdentity(MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), "conflict"),
        EventRevision(1, RevisionKind.ORIGINAL, 12),
        LifecycleState.SCHEDULED,
        ConfirmationState.DISPUTED,
        tuple(claim.observation_id for claim in claims),
        frozenset({SourceClass.OFFICIAL_PRIMARY, SourceClass.SECONDARY_NEWS}),
        "Conflicting schedule",
        scheduled_for_ns=100,
        quality_flags=frozenset({QualityFlag.CONFLICTING_SOURCES}),
    )
    resolved = ScheduleRevision(event, claims, official.observation_id)
    assert index.append(resolved)
    assert engine.resolve_schedule(event.event_id, 12).claims == claims
    assert engine.upcoming(20, horizon_ns=200).events[0].revision == resolved
    assert not index.append(resolved)
    with pytest.raises(PersistenceError):
        index.append(replace(resolved, adjudication_reason_codes=("DIFFERENT",)))
    with pytest.raises(PersistenceError):
        losing_event = replace(
            event,
            identity=EventIdentity(MACRO_CPI_US, event.identity.scope, "losing-claim"),
            scheduled_for_ns=110,
        )
        index.append(ScheduleRevision(losing_event, claims, news.observation_id))
    with pytest.raises(ValidationError):
        ScheduleRevision(replace(event, quality_flags=frozenset()), claims, official.observation_id)
    with pytest.raises(PersistenceError):
        other = schedule_revision(index, "different-conflict", 100, 12)
        wrong_class = replace(other.claims[0], source_class=SourceClass.UNVERIFIED)
        index.append(replace(other, claims=(wrong_class,)))
    assert index.store.get_observation(news.observation_id) == news


def test_equal_precedence_conflict_requires_reason() -> None:
    index, engine = engine_with_policy(EventWindowPolicy())
    first = raw_item("a", 10)
    second = raw_item("b", 10)
    index.append_observation(first)
    index.append_observation(second)
    claims = tuple(
        sorted(
            (
                ScheduleClaim(first.observation_id, SourceClass.OFFICIAL_PRIMARY, 100, 10),
                ScheduleClaim(second.observation_id, SourceClass.OFFICIAL_PRIMARY, 110, 10),
            ),
            key=lambda item: item.observation_id,
        )
    )
    event = CanonicalEvent(
        EventIdentity(MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), "equal"),
        EventRevision(1, RevisionKind.ORIGINAL, 11),
        LifecycleState.SCHEDULED,
        ConfirmationState.DISPUTED,
        tuple(claim.observation_id for claim in claims),
        frozenset({SourceClass.OFFICIAL_PRIMARY}),
        "Equal source conflict",
        scheduled_for_ns=100,
        quality_flags=frozenset({QualityFlag.CONFLICTING_SOURCES}),
    )
    chosen = next(claim for claim in claims if claim.proposed_time_ns == 100)
    with pytest.raises(PersistenceError):
        index.append(ScheduleRevision(event, claims, chosen.observation_id))
    adjudicated = ScheduleRevision(event, claims, chosen.observation_id, ("MANUAL_SOURCE_REVIEW",))
    index.append(adjudicated)
    assert engine.resolve_schedule(event.event_id, 11) == adjudicated


def test_coverage_distinguishes_empty_failed_stale_and_uningested() -> None:
    index, engine = engine_with_policy(EventWindowPolicy())
    empty = engine.events_between(100, 200, 50)
    assert empty.events == ()
    assert empty.coverage.status is CoverageStatus.NOT_INGESTED
    assert empty.outcome is QueryOutcome.INDETERMINATE
    source = SourceIdentity("calendar.example", "1", "1", SourceClass.OFFICIAL_PRIMARY)
    healthy = CoverageRecord(source, 100, 200, 40, SourceHealth(SourceHealthState.HEALTHY))
    assert index.append_coverage(healthy)
    assert not index.append_coverage(healthy)
    healthy_empty = engine.events_between(100, 200, 50)
    assert healthy_empty.coverage.status is CoverageStatus.AVAILABLE
    assert healthy_empty.outcome is QueryOutcome.NO_MATCH
    assert healthy_empty.fingerprint == engine.events_between(100, 200, 50).fingerprint
    unavailable = CoverageRecord(source, 100, 200, 60, SourceHealth(SourceHealthState.UNAVAILABLE))
    index.append_coverage(unavailable)
    assert engine.events_between(100, 200, 59).outcome is QueryOutcome.NO_MATCH
    failed = engine.events_between(100, 200, 60)
    assert failed.coverage.status is CoverageStatus.UNAVAILABLE
    assert failed.outcome is QueryOutcome.INDETERMINATE
    index.append_coverage(
        CoverageRecord(source, 100, 200, 70, SourceHealth(SourceHealthState.STALE))
    )
    assert engine.events_between(100, 200, 70).coverage.status is CoverageStatus.STALE
    other = SourceIdentity("secondary.example", "1", "1", SourceClass.SECONDARY_NEWS)
    index.append_coverage(
        CoverageRecord(other, 100, 200, 75, SourceHealth(SourceHealthState.HEALTHY))
    )
    assert engine.events_between(100, 200, 75).coverage.status is CoverageStatus.DEGRADED
    assert engine.events_between(100, 200, 75).coverage.source_states == (
        ("calendar.example", SourceHealthState.STALE),
        ("secondary.example", SourceHealthState.HEALTHY),
    )
    assert (
        engine.events_between(100, 200, 75, ApplicabilityQuery(country="CA")).coverage.status
        is CoverageStatus.DEGRADED
    )
    with pytest.raises(PersistenceError):
        index.append_coverage(replace(healthy, health=SourceHealth(SourceHealthState.DEGRADED)))


def test_coverage_scopes_and_future_health_exclusion() -> None:
    index, engine = engine_with_policy(EventWindowPolicy())
    source = SourceIdentity("macro.example", "1", "1", SourceClass.OFFICIAL_PRIMARY)
    scoped = CoverageRecord(
        source,
        100,
        200,
        80,
        SourceHealth(SourceHealthState.HEALTHY),
        EventScope(ScopeKind.MACRO_COUNTRY, ("US",)),
    )
    index.append_coverage(scoped)
    assert (
        engine.events_between(100, 200, 79, ApplicabilityQuery(country="US")).coverage.status
        is CoverageStatus.NOT_INGESTED
    )
    assert (
        engine.events_between(100, 200, 80, ApplicabilityQuery(country="US")).coverage.status
        is CoverageStatus.AVAILABLE
    )
    assert (
        engine.events_between(100, 200, 80, ApplicabilityQuery(country="CA")).coverage.status
        is CoverageStatus.NOT_INGESTED
    )
    assert engine.events_between(100, 200, 80).coverage.status is CoverageStatus.NOT_INGESTED
    index.append(schedule_revision(index, "known", 150, 90))
    assert (
        engine.events_between(100, 200, 90, ApplicabilityQuery(country="CA")).coverage.status
        is CoverageStatus.UNKNOWN
    )


def test_later_partial_coverage_cannot_certify_whole_interval() -> None:
    index, engine = engine_with_policy(EventWindowPolicy())
    source = SourceIdentity("calendar.example", "1", "1", SourceClass.OFFICIAL_PRIMARY)
    index.append_coverage(
        CoverageRecord(source, 100, 200, 40, SourceHealth(SourceHealthState.HEALTHY))
    )
    assert engine.events_between(100, 200, 50).outcome is QueryOutcome.NO_MATCH
    index.append_coverage(
        CoverageRecord(source, 150, 160, 60, SourceHealth(SourceHealthState.UNAVAILABLE))
    )
    whole = engine.events_between(100, 200, 60)
    assert whole.coverage.status is CoverageStatus.UNAVAILABLE
    assert whole.outcome is QueryOutcome.INDETERMINATE
    assert engine.events_between(150, 160, 60).coverage.status is CoverageStatus.UNAVAILABLE
    index.append_coverage(
        CoverageRecord(source, 150, 160, 70, SourceHealth(SourceHealthState.HEALTHY))
    )
    assert engine.events_between(100, 200, 70).coverage.status is CoverageStatus.UNKNOWN
    assert engine.events_between(150, 160, 70).coverage.status is CoverageStatus.AVAILABLE


def test_typed_result_contracts_reject_invalid_views() -> None:
    index, engine = engine_with_policy(EventWindowPolicy(1, 1, 1))
    revision = schedule_revision(index, "typed", 100, 10)
    index.append(revision)
    view = engine.upcoming(50, horizon_ns=100).events[0]
    result = engine.events_between(100, 101, 50)
    coverage = CoverageSummary(CoverageStatus.UNKNOWN)
    with pytest.raises(TemporalConsistencyError):
        ScheduledEventView(revision, 9)
    with pytest.raises(ValidationError):
        ScheduledEventView(None, 50)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CoverageSummary(CoverageStatus.UNKNOWN, (("x", "not-health"),))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ScheduledEventQueryResult(
            ScheduleQueryKind.UPCOMING, 50, 0, 200, (view, view), coverage, "config"
        )
    with pytest.raises(ValidationError):
        ScheduledEventQueryResult(
            ScheduleQueryKind.UPCOMING, 51, 0, 200, (view,), coverage, "config"
        )
    with pytest.raises(ValidationError):
        NextEventResult(101, view, coverage, "config")
    with pytest.raises(ValidationError):
        ActiveEventWindow(view, "PRE_EVENT")  # type: ignore[arg-type]
    active = ActiveEventWindow(view, EventWindowState.PRE_EVENT)
    with pytest.raises(ValidationError):
        ActiveWindowsResult(51, (active,), EventWindowState.PRE_EVENT, coverage, "config")
    assert result.fingerprint == engine.events_between(100, 101, 50).fingerprint


def test_schedule_claim_revision_and_index_validation() -> None:
    index, _ = engine_with_policy(EventWindowPolicy())
    original = schedule_revision(index, "contract", 100, 10)
    claim = original.claims[0]
    with pytest.raises(ValidationError):
        ScheduleClaim("bad", SourceClass.OFFICIAL_PRIMARY, 100, 10)
    with pytest.raises(ValidationError):
        ScheduleClaim(claim.observation_id, "official", 100, 10)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ScheduleRevision(
            replace(original.event, scheduled_for_ns=None), (claim,), claim.observation_id
        )
    with pytest.raises(ValidationError):
        ScheduleRevision(original.event, (), claim.observation_id)
    with pytest.raises(ValidationError):
        ScheduleRevision(original.event, (claim, claim), claim.observation_id)
    with pytest.raises(ValidationError):
        ScheduleRevision(original.event, (claim,), "obs_missing")
    with pytest.raises(TemporalConsistencyError):
        ScheduleRevision(
            original.event, (replace(claim, available_from_ns=11),), claim.observation_id
        )
    with pytest.raises(ValidationError):
        ScheduleRevision(
            original.event, (replace(claim, proposed_time_ns=101),), claim.observation_id
        )
    with pytest.raises(ValidationError):
        ScheduleRevision(original.event, (claim,), claim.observation_id, ("Z", "Z"))
    with pytest.raises(PersistenceError):
        index.append(
            replace(
                original,
                event=replace(original.event, evidence_observation_ids=("obs_absent",)),
                claims=(replace(claim, observation_id="obs_absent"),),
                selected_observation_id="obs_absent",
            )
        )
    assert index.append(original)
    later = schedule_revision(
        index,
        "contract",
        120,
        20,
        number=2,
        kind=RevisionKind.RESCHEDULE,
        lifecycle=LifecycleState.RESCHEDULED,
    )
    assert index.append(later)
    assert index.history(original.event_id) == (original, later)
    assert index.resolve(original.event_id, 19) == original
    with pytest.raises(PersistenceError):
        index.append(replace(later, adjudication_reason_codes=("DIFFERENT",)))


def test_coverage_record_validation_and_result_fingerprints() -> None:
    index, engine = engine_with_policy(EventWindowPolicy(1, 1, 1))
    source = SourceIdentity("coverage.example", "1", "1", SourceClass.OFFICIAL_PRIMARY)
    health = SourceHealth(SourceHealthState.HEALTHY, last_success_ns=50)
    with pytest.raises(TemporalConsistencyError):
        CoverageRecord(source, 100, 100, 60, health)
    with pytest.raises(TemporalConsistencyError):
        CoverageRecord(source, 100, 200, 40, health)
    with pytest.raises(ValidationError):
        CoverageRecord(source, 100, 200, 60, health, "US")  # type: ignore[arg-type]
    record = CoverageRecord(source, 100, 200, 60, health)
    index.append_coverage(record)
    assert index.coverage.records() == (record,)
    assert engine.next_event(100).fingerprint == engine.next_event(100).fingerprint
    assert engine.active_windows(100).fingerprint == engine.active_windows(100).fingerprint
    assert index.canonical_state_bytes() == index.canonical_state_bytes()


def test_repeated_builds_and_insertion_order_have_identical_state() -> None:
    states: list[bytes] = []
    results: list[bytes] = []
    for keys in (("alpha", "beta"), ("beta", "alpha")):
        index, engine = engine_with_policy(EventWindowPolicy(10, 5, 5))
        for key in keys:
            index.append(schedule_revision(index, key, 100, 10))
        states.append(index.canonical_state_bytes())
        results.append(canonical_json_bytes(engine.upcoming(50, horizon_ns=100)))
    assert states[0] == states[1]
    assert results[0] == results[1]


def test_window_policy_precedence_and_config_fingerprint() -> None:
    default = EventWindowPolicy(0, 0, 0)
    family = EventWindowPolicy(10, 5, 5)
    exact = EventWindowPolicy(20, 7, 8)
    windows = WindowPolicySet(default, ((EventFamily.MACRO, family),), ((MACRO_CPI_US, exact),))
    assert windows.policy_for(MACRO_CPI_US) == exact
    assert windows.policy_for(EventType(EventFamily.MACRO, "MACRO.NFP.US")) == family
    assert windows.policy_for(EventType(EventFamily.EARNINGS, "EARNINGS.RELEASE")) == default
    assert windows.max_pre_ns == 20
    assert windows.max_after_ns == 15
    settings = Settings(scheduled=ScheduledSettings(windows=windows))
    assert settings.fingerprint != Settings().fingerprint
    assert settings.fingerprint != Settings(scheduled=ScheduledSettings(max_results=5)).fingerprint
    assert settings.schema_version == "1.0"
    assert SCHEDULE_SCHEMA_VERSION == "1.0"
    assert SourcePrecedencePolicy().order_for("scheduled_for") == DEFAULT_PRECEDENCE
    with pytest.raises(ValidationError):
        WindowPolicySet(default, ((EventFamily.MACRO, family), (EventFamily.MACRO, exact)))
    with pytest.raises(ValidationError):
        WindowPolicySet(default, event_type_windows=((MACRO_CPI_US, exact), (MACRO_CPI_US, family)))
    with pytest.raises(TemporalConsistencyError):
        EventWindowPolicy(-1, 0, 0)
    with pytest.raises(ValidationError):
        EventWindowPolicy(0, MAX_NS, 1)
    with pytest.raises(ValidationError):
        ScheduledSettings(max_query_horizon_ns=0)
    with pytest.raises(ValidationError):
        ScheduledSettings(max_results=0)
    with pytest.raises(TemporalConsistencyError):
        window_state(MAX_NS, MAX_NS, EventWindowPolicy(0, 1, 0))


def test_query_limits_and_fail_closed_validation() -> None:
    settings = Settings(scheduled=ScheduledSettings(max_query_horizon_ns=100, max_results=1))
    index = ScheduledEventIndex(settings)
    engine = ScheduledEventEngine(index)
    index.append(schedule_revision(index, "one", 100, 10))
    index.append(schedule_revision(index, "two", 101, 10))
    with pytest.raises(ValidationError):
        engine.events_between(100, 102, 50)
    with pytest.raises(ValidationError):
        engine.events_between(0, 101, 50)
    with pytest.raises(ValidationError):
        engine.events_between(101, 100, 50)
    with pytest.raises(ValidationError):
        engine.upcoming(50, horizon_ns=101)
    with pytest.raises(ValidationError):
        engine.recent(200, 101)
    with pytest.raises(TemporalConsistencyError):
        engine.upcoming(-1)
    with pytest.raises(TemporalConsistencyError):
        index.candidate_ids(10, 9)
    assert engine.next_event(50).event.scheduled_for_ns == 100
    assert engine.window_state("evt_unknown", 50) is None


def test_aware_timezone_and_dst_normalization() -> None:
    ny = ZoneInfo("America/New_York")
    before = datetime(2026, 3, 8, 1, 30, tzinfo=ny)
    after = datetime(2026, 3, 8, 3, 30, tzinfo=ny)
    assert datetime_to_ns(after) - datetime_to_ns(before) == 3_600_000_000_000
    with pytest.raises(TemporalConsistencyError):
        datetime_to_ns(datetime(2026, 3, 8, 3, 30))
    policy = EventWindowPolicy(3_600_000_000_000, 1, 1)
    assert (
        window_state(datetime_to_ns(before), datetime_to_ns(after), policy)
        is EventWindowState.PRE_EVENT
    )
