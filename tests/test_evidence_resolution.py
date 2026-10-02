"""Independent sources, field conflicts, corrections and honest two-time history."""

from dataclasses import replace
from itertools import permutations

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.events.lifecycle import LifecycleState
from eie.evidence.claims import (
    Claim,
    ClaimSubject,
    OccurrenceValue,
    StatusValue,
    TimePrecision,
    TimeValue,
)
from eie.evidence.graph import ClaimRelationship, validate_relationship, validate_revision
from eie.evidence.models import ResolutionPolicy
from eie.evidence.resolution import resolve_claim
from eie.evidence.schema import (
    ClaimStatus,
    ClaimType,
    EvidenceRelation,
    Predicate,
    ResolutionState,
    SubjectKind,
)
from eie.expectations.models import EPS_DILUTED, MeasurementContext
from eie.expectations.units import Quantity, Unit, UnitKind
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput
from eie.sources.trust import SourceClass, SourcePrecedencePolicy
from tests.evidence_helpers import CLAIM, ISSUER, PERIOD, TIME, bare, item, setup, source


def changed_time(offset=1):
    return replace(TIME, timestamp_ns=TIME.timestamp_ns + offset)


def test_equal_precedence_conflict_is_unresolved():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, queries = setup(store)
        a, b = source("ALPHA"), source("BETA")
        engine.ingest(a, item(a, 100))
        engine.ingest(b, item(b, 200, changed_time()))
        result = queries.resolution(CLAIM.claim_id, 200)
        assert result.state is ResolutionState.UNRESOLVED
        assert result.value is None and result.selected_id is None
        assert result.summary.unresolved_conflicts == 1
        assert len(queries.contradictions(CLAIM.claim_id, 200)) == 2
        assert queries.supporting_claims(CLAIM.claim_id, 200) == ()
        assert queries.relationship_kind(CLAIM.claim_id, 200, EvidenceRelation.CONTRADICTS)
        assert repo.stored_resolution(result.resolution_id) == result


def test_correction_retraction_and_old_view():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, queries = setup(store)
        official = source("OFFICIAL", SourceClass.OFFICIAL_PRIMARY)
        secondary = source("SECONDARY")
        first = item(official, 100)
        engine.ingest(official, first)
        engine.ingest(secondary, item(secondary, 200))
        old = dumps(queries.resolution(CLAIM.claim_id, 200))
        correction = item(
            official, 300, changed_time(10), prior=first, status=ClaimStatus.CORRECTED
        )
        engine.ingest(official, correction)
        result = queries.resolution(CLAIM.claim_id, 300)
        assert result.value == correction.claims[0].value and result.summary.correction_count == 1
        assert result.conflicts and len(result.alternative_ids) == 3
        assert dumps(queries.resolution(CLAIM.claim_id, 200)) == old
        assert queries.claim_history(CLAIM.claim_id, 200)[0].known_to_ns is None
        assert queries.claim_history(CLAIM.claim_id, 300)[0].known_to_ns == 300
        withdrawal = item(
            official, 400, changed_time(10), prior=correction, status=ClaimStatus.RETRACTED
        )
        engine.ingest(official, withdrawal)
        assert queries.resolution(CLAIM.claim_id, 400).value == TIME
        assert queries.resolution(CLAIM.claim_id, 400).summary.retraction_count == 1
        assert len(queries.evidence_for_claim(CLAIM.claim_id, 400)) == 4
        assert queries.relationship_kind(CLAIM.claim_id, 400, EvidenceRelation.RETRACTS)
        assert repo.store.get_observation(first.raw.observation_id) == first.raw


@pytest.mark.parametrize("status", [ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN])
def test_all_retracted_preserves_history(status):
    with SQLiteEventStore(":memory:") as store:
        _, engine, queries = setup(store)
        provider = source("ALPHA")
        first = item(provider, 100)
        engine.ingest(provider, first)
        engine.ingest(provider, item(provider, 200, prior=first, status=status))
        assert queries.resolution(CLAIM.claim_id, 99).state is ResolutionState.ABSENT
        assert queries.resolution(CLAIM.claim_id, 100).value == TIME
        later = queries.resolution(CLAIM.claim_id, 200)
        assert later.state is ResolutionState.RETRACTED and len(later.alternative_ids) == 2


def test_same_provider_and_syndication_do_not_inflate_independence():
    with SQLiteEventStore(":memory:") as store:
        _, engine, queries = setup(store)
        a = source("PRIMARY", SourceClass.COMPANY_PRIMARY, family="issuer-release")
        b = source("MIRROR_B", family="issuer-release")
        c = source("MIRROR_C", family="issuer-release")
        first = item(a, 100)
        first_receipt = engine.ingest(a, first)
        assert engine.ingest(a, first) == first_receipt
        engine.ingest(a, item(a, 200, prior=first, status=ClaimStatus.UPDATED))
        engine.ingest(b, item(b, 300))
        engine.ingest(c, item(c, 400))
        result = queries.resolution(CLAIM.claim_id, 400)
        assert result.summary.independent_source_count == 1
        assert result.summary.confirmation_count == 0
        assert len(result.active_ids) == 3 and len(result.alternative_ids) == 4
        assert queries.relationship_kind(CLAIM.claim_id, 400, EvidenceRelation.SUPPORTS)
        assert not queries.relationship_kind(CLAIM.claim_id, 400, EvidenceRelation.CONFIRMS)


def test_two_independent_sources_and_temporal_refinement():
    with SQLiteEventStore(":memory:") as store:
        _, engine, queries = setup(store)
        a, b = source("ALPHA"), source("BETA")
        engine.ingest(a, item(a, 100, TimeValue(TimePrecision.DATE_ONLY, TIME.date)))
        engine.ingest(b, item(b, 200))
        result = queries.resolution(CLAIM.claim_id, 200)
        assert result.value == TIME and result.summary.independent_source_count == 2
        assert result.summary.confirmation_count == 1 and not result.conflicts
        assert "COMPATIBLE_REFINEMENT" in result.reasons
        assert queries.relationship_kind(CLAIM.claim_id, 200, EvidenceRelation.CONFIRMS)
        assert len(queries.supporting_claims(CLAIM.claim_id, 200)) == 2
        assert (
            queries.resolution(
                CLAIM.claim_id, 200, policy=ResolutionPolicy(prefer_exact_time=False)
            ).policy.prefer_exact_time
            is False
        )


def test_field_specific_precedence():
    with SQLiteEventStore(":memory:") as store:
        repo, _, queries = setup(store)
        from eie.evidence.engine import EvidenceEngine

        precedence = SourcePrecedencePolicy(
            fact_overrides=(
                (
                    "ACTUAL",
                    (
                        SourceClass.SECONDARY_NEWS,
                        *tuple(v for v in SourceClass if v is not SourceClass.SECONDARY_NEWS),
                    ),
                ),
            )
        )
        engine = EvidenceEngine(repo, resolution_policy=ResolutionPolicy(precedence))
        a, b = source("PRIMARY", SourceClass.OFFICIAL_PRIMARY), source("SECONDARY")
        context = MeasurementContext("evt_" + "a" * 64, EPS_DILUTED, PERIOD)
        q = Quantity(1, Unit(UnitKind.CURRENCY_PER_SHARE, currency="USD"))
        numeric = Claim(
            ClaimType.ACTUAL_VALUE,
            ClaimSubject(SubjectKind.MEASUREMENT, context.event_id),
            Predicate.ACTUAL,
            context,
            q.canonical_unit,
        )
        for provider, known, value in ((a, 100, q), (b, 200, Quantity(2, q.unit))):
            batch = item(provider, known)
            engine.ingest(
                provider,
                replace(
                    batch,
                    claims=(
                        ClaimInput(
                            "actual",
                            numeric,
                            value,
                            (batch.references[0].evidence_id,),
                            event_id=batch.events[0].event_id,
                        ),
                        *batch.claims,
                    ),
                ),
            )
        assert (
            queries.resolution(CLAIM.claim_id, 200, policy=engine.resolution_policy).value == TIME
        )
        assert queries.resolution(
            numeric.claim_id, 200, policy=engine.resolution_policy
        ).value == Quantity(2, q.unit)
        assert (
            len(
                queries.claims(
                    200, subject=CLAIM.subject.identity, predicate=Predicate.SCHEDULED_FOR
                )
            )
            == 1
        )
        assert len(queries.claims_as_of(200)) == 2
        assert len(queries.event_claims(item(a, 100).events[0].event_id, 200)) == 2


def test_valid_windows_and_semantic_binding_correction():
    with SQLiteEventStore(":memory:") as store:
        _, engine, queries = setup(store)
        p = source("ALPHA")
        first = bare(p, 100)
        oldclaim = first.claims[0].claim
        engine.ingest(
            p, replace(first, claims=(replace(first.claims[0], valid_from_ns=10, valid_to_ns=20),))
        )
        assert queries.resolution(oldclaim.claim_id, 100, 9).state is ResolutionState.ABSENT
        assert queries.resolution(oldclaim.claim_id, 100, 10).value == OccurrenceValue(True)
        assert queries.resolution(oldclaim.claim_id, 100, 20).state is ResolutionState.ABSENT
        newclaim = Claim(
            ClaimType.EVENT_OCCURRENCE,
            ClaimSubject(SubjectKind.ENTITY, "iss_" + "b" * 64),
            Predicate.OCCURRED,
        )
        second = bare(p, 200)
        engine.ingest(
            p,
            replace(
                second,
                claims=(replace(second.claims[0], claim=newclaim, status=ClaimStatus.CORRECTED),),
            ),
        )
        assert queries.resolution(oldclaim.claim_id, 200).state is ResolutionState.ABSENT
        assert queries.resolution(newclaim.claim_id, 200).value == OccurrenceValue(True)
        assert queries.resolution(oldclaim.claim_id, 100).value == OccurrenceValue(True)


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_insertion_order_determinism(order):
    providers = (source("ALPHA"), source("BETA"), source("GAMMA"))
    values = (TIME, TIME, changed_time())
    with SQLiteEventStore(":memory:") as store:
        _, engine, queries = setup(store)
        for i in order:
            engine.ingest(providers[i], item(providers[i], 100, values[i]))
        actual = dumps(queries.resolution(CLAIM.claim_id, 100))
        groups = dumps(queries.event_resolution(100))
    with SQLiteEventStore(":memory:") as store:
        _, engine, queries = setup(store)
        for i in range(3):
            engine.ingest(providers[i], item(providers[i], 100, values[i]))
        assert dumps(queries.resolution(CLAIM.claim_id, 100)) == actual
        assert dumps(queries.event_resolution(100)) == groups


@pytest.mark.parametrize("relation", list(EvidenceRelation))
def test_relationship_taxonomy_and_fail_closed(relation):
    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        a, b = source("ALPHA"), source("BETA")
        first = item(a, 100)
        ra = engine.ingest(a, first)
        rb = engine.ingest(b, item(b, 200))
        left, right = repo.assertion(ra.assertion_ids[0]), repo.assertion(rb.assertion_ids[0])
        if relation in (
            EvidenceRelation.CORRECTS,
            EvidenceRelation.UPDATES,
            EvidenceRelation.RETRACTS,
            EvidenceRelation.SUPERSEDES,
        ):
            status = (
                ClaimStatus.CORRECTED
                if relation is EvidenceRelation.CORRECTS
                else ClaimStatus.RETRACTED
                if relation is EvidenceRelation.RETRACTS
                else ClaimStatus.UPDATED
            )
            rc = engine.ingest(a, item(a, 300, prior=first, status=status))
            right = repo.assertion(rc.assertion_ids[0])
        if relation is EvidenceRelation.CONTRADICTS:
            right = replace(right, value=changed_time())
        link = ClaimRelationship(
            right.observation_id, left.observation_id, relation, right.known_from_ns
        )
        validate_relationship(link, right, left)
        with pytest.raises(ValidationError):
            validate_relationship(replace(link, known_from_ns=0), right, left)
        with pytest.raises(ValidationError):
            validate_relationship(link, left, right)


def test_revision_errors_status_transitions_and_resolution_bounds():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        p = source("ALPHA")
        batch = bare(p)
        claim = Claim(
            ClaimType.EVENT_STATUS, ClaimSubject(SubjectKind.ENTITY, ISSUER), Predicate.STATUS
        )
        initial = replace(
            batch,
            claims=(
                replace(batch.claims[0], claim=claim, value=StatusValue(LifecycleState.SCHEDULED)),
            ),
        )
        receipt = engine.ingest(p, initial)
        previous = repo.assertion(receipt.assertion_ids[0])
        current = replace(
            previous,
            number=2,
            previous_id=previous.observation_id,
            known_from_ns=200,
            status=ClaimStatus.UPDATED,
            value=StatusValue(LifecycleState.RELEASED),
        )
        validate_revision(current, previous)
        for change in (
            {"number": 3},
            {"known_from_ns": 100},
            {"previous_id": "pclaim_" + "b" * 64},
            {"status": ClaimStatus.ASSERTED},
            {"source_family": "other"},
        ):
            with pytest.raises(ValidationError):
                validate_revision(replace(current, **change), previous)
        with pytest.raises(ValidationError):
            validate_revision(current, None)
        with pytest.raises(ValidationError):
            resolve_claim(claim, "eds_" + "b" * 64, (previous,), (), 100)
        with pytest.raises(PersistenceError):
            repo.claim("missing")
