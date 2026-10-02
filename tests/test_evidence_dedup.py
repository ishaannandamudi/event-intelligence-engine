"""Adversarial structured matching, bounded blocks and merge/split replay."""

from dataclasses import replace
from itertools import permutations

import pytest

from eie.core.errors import ValidationError
from eie.corporate.models import CorporateIdentity
from eie.corporate.types import CorporateKind, CorporateStatus, EarningsPeriod
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.claims import ClaimSubject, TimePrecision, TimeValue
from eie.evidence.dedup import EventResolutionGroup, resolve_events
from eie.evidence.matching import DedupePolicy, EventParty, PartyRole, blocks, match_events
from eie.evidence.models import EvidenceLimits
from eie.evidence.schema import ClaimStatus, DedupeDecision, MatchReason, SubjectKind
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.common.models import PeriodFrequency, ReferencePeriod
from tests.evidence_helpers import DATASET, ISSUER, TIME, item, setup, source


def descriptors():
    return item(source("ALPHA"), 100).descriptors[0], item(source("BETA"), 100).descriptors[0]


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {
                "event_type": EventType(
                    EventFamily.MANAGEMENT_GOVERNANCE, "MANAGEMENT.CEO_DEPARTURE"
                ),
                "corporate_identity": None,
            },
            MatchReason.INCOMPATIBLE_TYPE,
        ),
        (
            {
                "entities": (ClaimSubject(SubjectKind.ENTITY, "iss_" + "b" * 64),),
                "corporate_identity": None,
            },
            MatchReason.DIFFERENT_ENTITY,
        ),
        (
            {"period": EarningsPeriod(2026, 2, "2026-06-30"), "corporate_identity": None},
            MatchReason.DIFFERENT_PERIOD,
        ),
        ({"effective_date": "2026-11-01"}, MatchReason.DISTINCT_EFFECTIVE_DATE),
        ({"release_stage": "FINAL"}, MatchReason.DISTINCT_RELEASE_STAGE),
        ({"lifecycle": CorporateStatus.TERMINATED}, MatchReason.INCOMPATIBLE_LIFECYCLE),
    ],
)
def test_false_merge_guards(changes, reason):
    a, b = descriptors()
    a = replace(
        a,
        effective_date="2026-10-30",
        release_stage="PRELIMINARY",
        lifecycle=CorporateStatus.CLOSED,
    )
    b = replace(b, **changes)
    result = match_events(a, b)
    assert result.decision is DedupeDecision.KEEP_SEPARATE and reason in result.conflicts
    assert match_events(b, a) == result


def test_two_sec_filings_same_day_never_collapse_and_mirror_merges():
    a, b = descriptors()
    event_type = EventType(EventFamily.SEC_FILING, "SEC.8_K")
    a = replace(
        a,
        event_type=event_type,
        corporate_identity=None,
        period=None,
        accession="0000320193-26-000001",
    )
    b = replace(
        b,
        event_type=event_type,
        corporate_identity=None,
        period=None,
        accession="0000320193-26-000002",
    )
    assert match_events(a, b).decision is DedupeDecision.KEEP_SEPARATE
    assert MatchReason.DISTINCT_ACCESSION in match_events(a, b).conflicts
    b = replace(b, accession=a.accession)
    assert match_events(a, b).decision is DedupeDecision.MERGE
    assert set(blocks(a)) & set(blocks(b))


def test_distinct_dividends_and_same_normalized_corporate_identity():
    a, b = descriptors()
    typ = EventType(EventFamily.CORPORATE_ACTION, "CORPORATE_ACTION.DIVIDEND")
    identity = CorporateIdentity(CorporateKind.DIVIDEND, ISSUER, "ex-date:2026-10-30")
    a = replace(
        a, event_type=typ, period=None, corporate_identity=identity, effective_date="2026-10-30"
    )
    b = replace(
        b, event_type=typ, period=None, corporate_identity=identity, effective_date="2026-10-30"
    )
    assert match_events(a, b).decision is DedupeDecision.MERGE
    b = replace(
        b,
        corporate_identity=replace(identity, semantic_key="ex-date:2026-11-01"),
        effective_date="2026-11-01",
    )
    assert match_events(a, b).decision is DedupeDecision.KEEP_SEPARATE
    b = replace(b, corporate_identity=None, effective_date=a.effective_date)
    a = replace(a, corporate_identity=None)
    assert match_events(a, b).decision is DedupeDecision.UNRESOLVED


def test_deal_roles_and_counterparties():
    a, b = descriptors()
    target = ClaimSubject(SubjectKind.ENTITY, ISSUER)
    acquirer = ClaimSubject(SubjectKind.ENTITY, "iss_" + "b" * 64)
    third = ClaimSubject(SubjectKind.ENTITY, "iss_" + "c" * 64)
    typ = EventType(EventFamily.CORPORATE_ACTION, "CORPORATE_ACTION.ACQUISITION")
    parties = (EventParty(PartyRole.ACQUIRER, acquirer), EventParty(PartyRole.TARGET, target))
    identity = CorporateIdentity(CorporateKind.ACQUISITION, ISSUER, "deal-1")
    kwargs = {
        "event_type": typ,
        "period": None,
        "corporate_identity": identity,
        "parties": parties,
        "entities": tuple(sorted((target, acquirer), key=lambda e: e.identity)),
    }
    a, b = replace(a, **kwargs), replace(b, **kwargs)
    assert match_events(a, b).decision is DedupeDecision.MERGE
    b = replace(
        b,
        parties=(EventParty(PartyRole.ACQUIRER, third), EventParty(PartyRole.TARGET, target)),
        entities=tuple(sorted((target, third), key=lambda e: e.identity)),
    )
    assert MatchReason.CONFLICTING_PARTIES in match_events(a, b).conflicts
    assert match_events(a, b).decision is DedupeDecision.KEEP_SEPARATE
    b = replace(b, parties=())
    assert (
        match_events(
            a,
            replace(
                a,
                source=source("OTHER").source,
                provider_event_key="unknown",
                event_id="evt_" + "b" * 64,
                parties=(),
            ),
        ).decision
        is DedupeDecision.UNRESOLVED
    )


def test_macro_series_period_stage_and_official_source():
    a, b = descriptors()
    period = ReferencePeriod("2026-09-01", "2026-10-01", PeriodFrequency.MONTHLY, "2026-09")
    typ = EventType(EventFamily.MACRO, "MACRO.CPI.US")
    kwargs = {
        "event_type": typ,
        "period": period,
        "corporate_identity": None,
        "entities": (),
        "official_release_id": "BLS-CPI-2026-09",
        "release_stage": "INITIAL",
    }
    a, b = replace(a, **kwargs), replace(b, **kwargs)
    assert match_events(a, b).decision is DedupeDecision.MERGE
    assert set(blocks(a)) & set(blocks(b))
    assert (
        match_events(
            a, replace(b, event_type=EventType(EventFamily.MACRO, "MACRO.NFP.US"))
        ).decision
        is DedupeDecision.KEEP_SEPARATE
    )
    assert (
        match_events(a, replace(b, release_stage="REVISED")).decision
        is DedupeDecision.KEEP_SEPARATE
    )
    assert (
        match_events(a, replace(b, official_release_id=None)).decision is DedupeDecision.UNRESOLVED
    )


def test_temporal_tolerance_coarse_time_reschedule_and_missing_identity():
    a, b = descriptors()
    a, b = replace(a, corporate_identity=None), replace(b, corporate_identity=None)
    assert match_events(a, b).decision is DedupeDecision.MERGE
    assert (
        match_events(a, replace(b, time=TimeValue(TimePrecision.DATE_ONLY, TIME.date))).decision
        is DedupeDecision.MERGE
    )
    assert (
        MatchReason.STABLE_IDENTITY_TIME_CHANGE
        in match_events(
            a, replace(b, time=replace(TIME, timestamp_ns=TIME.timestamp_ns + 360_000_000_000))
        ).matches
    )
    a, b = replace(a, period=None), replace(b, period=None)
    assert match_events(a, b).decision is DedupeDecision.UNRESOLVED
    changed = replace(b, time=replace(TIME, timestamp_ns=TIME.timestamp_ns + 1))
    assert match_events(a, changed, DedupePolicy(())).decision is DedupeDecision.KEEP_SEPARATE
    policy = DedupePolicy(((a.event_type.code, 10),))
    result = match_events(a, changed, policy)
    assert (
        result.decision is DedupeDecision.UNRESOLVED
        and MatchReason.TIME_WITHIN_TOLERANCE in result.matches
    )
    assert match_events(a, replace(b, event_id=a.event_id)).decision is DedupeDecision.MERGE
    assert (
        match_events(
            a, replace(b, source=a.source, provider_event_key=a.provider_event_key)
        ).decision
        is DedupeDecision.MERGE
    )
    with pytest.raises(ValidationError):
        match_events(a, a)
    with pytest.raises(ValidationError):
        match_events(a, replace(b, dataset_id="eds_" + "b" * 64))


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_complete_link_prevents_transitive_false_merge(order):
    a, b = descriptors()
    # Use unrelated action IDs on an otherwise equal earnings-period block to
    # exercise the guard independently of a provider normalization convention.
    a = replace(a, corporate_identity=None)
    b = replace(b, corporate_identity=None)
    c = replace(b, source=source("GAMMA").source, event_id="evt_" + "b" * 64)
    a = replace(a, effective_date="2026-10-30")
    c = replace(c, effective_date="2026-11-01")
    # A/B and B/C merge via exact issuer/period; A/C have distinct action dates.
    rows = (a, b, c)
    run, _ = resolve_events(tuple(rows[i] for i in order), DATASET.dataset_id, 100)
    assert not any(len(g.member_event_ids) == 3 for g in run.groups)
    assert len(run.groups) >= 2


def test_blocking_bounded_candidates_and_group_bound():
    a, b = descriptors()
    rows = tuple(
        replace(b, source=source(f"P{i}").source, event_id="evt_" + format(i, "064x"))
        for i in range(12)
    )
    run, candidates = resolve_events(
        rows, DATASET.dataset_id, 100, limits=EvidenceLimits(candidates_per_event=2)
    )
    assert run.truncated_event_ids
    assert all(
        sum(d in (c.left_id, c.right_id) for c in candidates) <= 2 for d in run.descriptor_ids
    )
    assert all(len(g.member_event_ids) == 1 for g in run.groups)
    run, _ = resolve_events((a, b), DATASET.dataset_id, 100, limits=EvidenceLimits(group_members=1))
    assert len(run.groups) == 2 and run.truncated_event_ids
    unrelated = tuple(
        replace(
            r,
            entities=(ClaimSubject(SubjectKind.ENTITY, "iss_" + format(i, "064x")),),
            corporate_identity=None,
        )
        for i, r in enumerate(rows)
    )
    assert resolve_events(unrelated, DATASET.dataset_id, 100)[1] == ()
    assert resolve_events((a, b), DATASET.dataset_id, 99)[0].groups == ()
    with pytest.raises(ValidationError):
        resolve_events((a, b), DATASET.dataset_id, 100, limits=EvidenceLimits(query_size=1))


def test_merge_split_and_split_merge_history():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, queries = setup(store)
        a, b = source("ALPHA"), source("BETA")
        first = item(a, 100)
        second = item(b, 200)
        engine.ingest(a, first)
        # A weaker source lacks exact reporting identity initially.
        weak = replace(
            second,
            descriptors=(replace(second.descriptors[0], period=None, corporate_identity=None),),
        )
        engine.ingest(b, weak)
        assert len(queries.event_resolution(200).groups) == 2
        before = dumps(queries.event_resolution(200))
        third = item(b, 300, prior=weak, status=ClaimStatus.UPDATED)
        engine.ingest(b, third)
        assert len(queries.event_resolution(300).groups) == 1
        merged = dumps(queries.event_resolution(300))
        fourth = item(b, 400, prior=third, status=ClaimStatus.CORRECTED)
        descriptor = replace(
            fourth.descriptors[0],
            period=EarningsPeriod(2026, 2, "2026-06-30"),
            corporate_identity=None,
        )
        engine.ingest(b, replace(fourth, descriptors=(descriptor,)))
        assert len(queries.event_resolution(400).groups) == 2
        assert dumps(queries.event_resolution(200)) == before
        assert dumps(queries.event_resolution(300)) == merged
        assert len(queries.event_resolution_history(400)) == 4
        assert repo.store.get_revisions(second.events[0].event_id)[0] == second.events[0]
        assert queries.resolved_event(first.events[0].event_id, 300)
        assert len(queries.event_sources(first.events[0].event_id, 300)) >= 2


@pytest.mark.parametrize(
    "changes",
    [
        {"event_revision": 0},
        {"number": 2},
        {"accession": "bad"},
        {
            "entities": (),
            "corporate_identity": CorporateIdentity(CorporateKind.DIVIDEND, ISSUER, "x"),
        },
        {"effective_date": "bad"},
        {
            "parties": (
                EventParty(PartyRole.TARGET, ClaimSubject(SubjectKind.ENTITY, "iss_" + "b" * 64)),
            )
        },
    ],
)
def test_invalid_descriptors(changes):
    with pytest.raises(ValidationError):
        replace(descriptors()[0], **changes)


def test_group_contract_requires_exact_membership():
    a, b = descriptors()
    with pytest.raises(ValidationError):
        EventResolutionGroup(
            (a.event_id,), (a.descriptor_id,), b.event_id, DedupePolicy().fingerprint
        )
    run, _ = resolve_events((a, b), DATASET.dataset_id, 100)
    with pytest.raises(ValidationError):
        replace(run, descriptor_ids=())
