"""Independent boundary checks for unresolved fields, causal artifacts and source data."""

from dataclasses import replace
from hashlib import sha256

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.corporate.types import CorporateStatus
from eie.events.revisions import EventRevision, RevisionKind
from eie.evidence.claims import (
    Claim,
    ClaimSubject,
    MeasurementSemantics,
    NumericRange,
    StatusValue,
    TimePrecision,
    TimeValue,
)
from eie.evidence.content import verify_fragments
from eie.evidence.dedup import resolve_events
from eie.evidence.graph import ClaimRelationship, validate_relationship, validate_revision
from eie.evidence.matching import match_events
from eie.evidence.models import ClaimConflict
from eie.evidence.schema import (
    ClaimStatus,
    ClaimType,
    ConflictType,
    EvidenceRelation,
    MatchReason,
    Predicate,
    ResolutionState,
    SubjectKind,
)
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import (
    CPI_HEADLINE_MOM,
    EPS_DILUTED,
    GUIDANCE_EPS,
    MeasurementContext,
    ObservationSource,
)
from eie.expectations.observations import (
    ActualObservation,
    ActualRole,
    EstimateKind,
    EstimateObservation,
)
from eie.expectations.units import Quantity, Unit, UnitKind
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.common.models import PeriodFrequency, ReferencePeriod
from eie.providers.evidence.normalization import guidance_claim, numeric_claim
from eie.sources.trust import SourceClass
from tests.evidence_helpers import CLAIM, DATASET, ISSUER, PERIOD, TIME, bare, item, setup, source


def observation_pair(repo, engine):
    a, b = source("ALPHA"), source("BETA")
    first = item(a, 100)
    second = item(b, 200)
    ra = engine.ingest(a, first)
    rb = engine.ingest(b, second)
    return repo.assertion(ra.assertion_ids[0]), repo.assertion(rb.assertion_ids[0])


def test_unknown_time_never_confirms_or_contradicts():
    with SQLiteEventStore(":memory:") as store:
        _, engine, queries = setup(store)
        a, b = source("ALPHA", SourceClass.OFFICIAL_PRIMARY), source("BETA")
        engine.ingest(a, item(a, 100, TimeValue(TimePrecision.UNKNOWN)))
        first = queries.resolution(CLAIM.claim_id, 100)
        assert first.state is ResolutionState.UNRESOLVED and not first.conflicts
        engine.ingest(b, item(b, 200))
        result = queries.resolution(CLAIM.claim_id, 200)
        assert result.value == TIME and not result.conflicts
        assert result.summary.independent_source_count == 1
        assert result.relationship_ids == ()


@pytest.mark.parametrize(
    "kind", (EvidenceRelation.SUPPORTS, EvidenceRelation.CONFIRMS, EvidenceRelation.CONTRADICTS)
)
def test_explicit_links_cannot_treat_unknown_time_as_evidence(kind):
    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        a, b = observation_pair(repo, engine)
        a = replace(a, value=TimeValue(TimePrecision.UNKNOWN))
        if kind is not EvidenceRelation.CONTRADICTS:
            b = replace(b, value=a.value)
        with pytest.raises(ValidationError, match="unknown time"):
            validate_relationship(
                ClaimRelationship(a.observation_id, b.observation_id, kind, 200), a, b
            )


def test_csv_evidence_rejects_ambiguous_headers():
    from eie.evidence.references import EvidenceLocator, LocatorKind

    ref = replace(
        item(source("ALPHA"), 100).references[0],
        locator=EvidenceLocator(LocatorKind.CSV_FIELD, "value", row=0),
        fragment_hash=sha256(canonical_json_bytes("second")).hexdigest(),
    )
    with pytest.raises(ValidationError, match="invalid structured evidence fragment"):
        verify_fragments(b"value,value\nfirst,second\n", (ref,))


@pytest.mark.parametrize(
    "relation",
    (
        EvidenceRelation.SUPPORTS,
        EvidenceRelation.CONFIRMS,
        EvidenceRelation.CONTRADICTS,
        EvidenceRelation.UPDATES,
        EvidenceRelation.CORRECTS,
        EvidenceRelation.RETRACTS,
    ),
)
def test_semantically_invalid_relationships_rejected(relation):
    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        a, b = observation_pair(repo, engine)
        if relation in (EvidenceRelation.SUPPORTS, EvidenceRelation.CONFIRMS):
            b = replace(b, value=replace(TIME, timestamp_ns=TIME.timestamp_ns + 1))
        link = ClaimRelationship(b.observation_id, a.observation_id, relation, 200)
        with pytest.raises(ValidationError):
            validate_relationship(link, b, a)
        if relation is EvidenceRelation.CONFIRMS:
            b = replace(b, value=TIME, source_family=a.source_family)
            with pytest.raises(ValidationError):
                validate_relationship(replace(link, source_id=b.observation_id), b, a)
        if relation is EvidenceRelation.SUPPORTS:
            b = replace(b, claim_id="cclaim_" + "b" * 64)
            with pytest.raises(ValidationError):
                validate_relationship(replace(link, source_id=b.observation_id), b, a)


def test_relation_action_status_mismatch_and_corporate_transitions():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        a, _ = observation_pair(repo, engine)
        current = replace(
            a, number=2, previous_id=a.observation_id, known_from_ns=300, status=ClaimStatus.UPDATED
        )
        link = ClaimRelationship(
            current.observation_id, a.observation_id, EvidenceRelation.CORRECTS, 300
        )
        with pytest.raises(ValidationError):
            validate_relationship(link, current, a)
        with pytest.raises(ValidationError):
            validate_revision(replace(current, claim_id="cclaim_" + "b" * 64), a)
        previous = replace(a, value=StatusValue(CorporateStatus.ANNOUNCED))
        for status in (CorporateStatus.ANNOUNCED, CorporateStatus.PENDING, CorporateStatus.CLOSED):
            current = replace(
                previous,
                number=2,
                previous_id=previous.observation_id,
                known_from_ns=300,
                status=ClaimStatus.UPDATED,
                value=StatusValue(status),
            )
            validate_revision(current, previous)
        with pytest.raises(ValidationError):
            validate_revision(
                replace(current, value=StatusValue(CorporateStatus.SCHEDULED)), previous
            )
        from eie.events.lifecycle import LifecycleState

        with pytest.raises(ValidationError):
            validate_revision(
                replace(current, value=StatusValue(LifecycleState.SCHEDULED)), previous
            )


def test_all_numeric_roles_and_guidance_keep_semantic_dimensions():
    raw_id = "obs_" + "a" * 64
    event_id = "evt_" + "a" * 64
    origin = ObservationSource(
        "SYNTH_A", "item", raw_id, SourceClass.OFFICIAL_PRIMARY, 100, 100, 100
    )
    revision = EventRevision(1, RevisionKind.ORIGINAL, 100)
    quantity = Quantity(1, Unit(UnitKind.CURRENCY_PER_SHARE, currency="USD"))
    context = MeasurementContext(event_id, EPS_DILUTED, PERIOD)
    actual = ActualObservation(context, origin, quantity, ActualRole.ACTUAL, 100, revision)
    previous = replace(actual, role=ActualRole.PREVIOUS, previous_reference_period=PERIOD)
    assert numeric_claim(actual).claim_id != numeric_claim(previous).claim_id
    estimate = EstimateObservation(
        context, origin, quantity, EstimateKind.PROVIDER_CONSENSUS_MEAN, revision
    )
    median = replace(estimate, estimate_type=EstimateKind.PROVIDER_CONSENSUS_MEDIAN)
    assert numeric_claim(estimate).claim_id != numeric_claim(median).claim_id
    monthly = ReferencePeriod("2026-09-01", "2026-10-01", PeriodFrequency.MONTHLY, "Sep2026")
    macro = replace(
        actual,
        context=MeasurementContext(event_id, CPI_HEADLINE_MOM, monthly),
        value=Quantity(0.2, Unit(UnitKind.PERCENT)),
    )
    assert numeric_claim(macro).claim_type is ClaimType.MACRO_VALUE
    guidance = GuidanceObservation(
        MeasurementContext(event_id, GUIDANCE_EPS, PERIOD),
        ISSUER,
        origin,
        revision,
        100,
        low=quantity,
    )
    claim, value = guidance_claim(guidance)
    claim.validate_value(value)
    assert value.upper is None and value.lower == quantity
    point = guidance_claim(replace(guidance, low=None, point=quantity))[1]
    assert point.point == quantity and point.canonical_unit == quantity.canonical_unit
    assert dumps(numeric_claim(actual)) == dumps(numeric_claim(actual))
    for semantics in (
        {},
        {"actual_role": ActualRole.PREVIOUS},
        {"actual_role": ActualRole.ACTUAL, "previous_period": PERIOD},
        {"estimate_kind": EstimateKind.PROVIDER_CONSENSUS_MEAN, "contributor_id": "analyst"},
    ):
        with pytest.raises(ValidationError):
            MeasurementSemantics(**semantics)
    with pytest.raises(ValidationError):
        NumericRange(None, None)
    with pytest.raises(ValidationError):
        NumericRange(quantity, Quantity(2, quantity.unit), Quantity(3, quantity.unit))
    assert NumericRange(None, quantity).canonical_unit == quantity.canonical_unit
    from eie.evidence.resolution import compatible

    assert compatible(point, point)
    assert not compatible(value, point)
    with pytest.raises(ValidationError):
        replace(numeric_claim(actual), claim_type=ClaimType.MACRO_VALUE)
    with pytest.raises(ValidationError):
        replace(
            numeric_claim(actual),
            measurement_semantics=MeasurementSemantics(
                estimate_kind=EstimateKind.PROVIDER_CONSENSUS_MEAN
            ),
        )


def test_resolution_artifacts_reject_incomplete_future_and_forged_inputs():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, queries = setup(store)
        a, b = observation_pair(repo, engine)
        result = queries.resolution(CLAIM.claim_id, 200)
        rows = repo.observations(DATASET.dataset_id, 200, claim_id=CLAIM.claim_id)
        links = repo.relationships(DATASET.dataset_id, 200, tuple(r.observation_id for r in rows))
        with pytest.raises(ValidationError), store.transaction():
            repo.append_resolution(result, (a,), ())
        with pytest.raises(ValidationError), store.transaction():
            repo.append_resolution(result, (*rows, replace(b, known_from_ns=201)), links)
        with pytest.raises(ValidationError), store.transaction():
            repo.append_resolution(replace(result, reasons=("forged",)), rows, links)
        run = queries.event_resolution(200)
        subset, _ = resolve_events(
            (repo.descriptor(run.descriptor_ids[0]),), DATASET.dataset_id, 200
        )
        with pytest.raises(ValidationError), store.transaction():
            repo.append_event_run(subset, ())
        with pytest.raises(ValidationError), store.transaction():
            repo.append_event_run(run, ())
        assert len(queries.event_field_resolutions(run.groups[0].member_event_ids[0], 200)) == 1
        assert queries.event_group("evt_" + "b" * 64, 200) is None
        assert queries.resolved_event("evt_" + "b" * 64, 200) is None
        assert queries.resolution_as_of(200) == run
        assert queries.event_sources("evt_" + "b" * 64, 200) == ()
        full = queries.traverse(a.raw_observation_id, 200, max_results=5)
        assert len(full.assertion_ids) == 2 and not full.truncated
        limited = queries.traverse(a.raw_observation_id, 200, max_results=1)
        assert limited.truncated
        for changes in (
            {"active_ids": ("pclaim_" + "b" * 64,)},
            {"value": None},
            {"state": ResolutionState.UNRESOLVED, "selected_id": None},
        ):
            with pytest.raises(ValidationError):
                replace(result, **changes)
        with pytest.raises(ValidationError):
            ClaimConflict("b", "a", ConflictType.VALUE)
        with pytest.raises(ValidationError):
            replace(result.summary, confirmation_count=-1)


def test_same_event_hard_conflict_rejected_and_duplicate_groups_bounded():
    a = item(source("ALPHA"), 100).descriptors[0]
    b = replace(item(source("BETA"), 100).descriptors[0], event_id=a.event_id)
    run, _ = resolve_events((a, b), DATASET.dataset_id, 100)
    assert len(run.groups) == 1
    from eie.evidence.models import EvidenceLimits

    with pytest.raises(ValidationError):
        resolve_events((a, b), DATASET.dataset_id, 100, limits=EvidenceLimits(group_members=1))
    with pytest.raises(ValidationError):
        resolve_events(
            (
                a,
                replace(b, effective_date="2026-11-01"),
                replace(a, source=source("GAMMA").source, effective_date="2026-10-30"),
            ),
            DATASET.dataset_id,
            100,
        )
    candidate = match_events(a, b)
    for changes in (
        {"left_id": candidate.right_id},
        {"time_delta_ns": -1},
        {"matches": (*candidate.matches, *candidate.matches)},
        {"conflicts": (MatchReason.DIFFERENT_ENTITY,)},
    ):
        with pytest.raises(ValidationError):
            replace(candidate, **changes)


@pytest.mark.parametrize(
    "payload,path",
    [
        (b"<!DOCTYPE root><root/>", "/root"),
        (b"<root><x>1</x><x>2</x></root>", "/root/x"),
        (b"<root/>", "/other"),
        (b"<root/>", "/root//x"),
    ],
)
def test_xml_external_entities_and_ambiguous_paths_fail(payload, path):
    from eie.evidence.references import EvidenceLocator, LocatorKind

    ref = replace(
        item(source("ALPHA"), 100).references[0],
        locator=EvidenceLocator(LocatorKind.XML_PATH, path),
    )
    with pytest.raises(ValidationError):
        verify_fragments(payload, (ref,))


def test_successful_content_verified_ingestion_and_no_raw_storage():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        p = source("ALPHA")
        batch = bare(p)
        payload = canonical_json_bytes({"occurred": True})
        raw = replace(batch.raw, payload_hash=sha256(payload).hexdigest())
        from eie.evidence.references import EvidenceLocator, LocatorKind
        from eie.providers.evidence.normalization import field_reference

        ref = field_reference(
            raw.observation_id,
            raw.source_item_id,
            raw.payload_hash,
            p.parser_version,
            "1",
            EvidenceLocator(LocatorKind.FIELD, "occurred"),
            True,
        )
        changed = replace(
            batch,
            raw=raw,
            lineage=replace(batch.lineage, raw_observation_id=raw.observation_id),
            references=(ref,),
            claims=(replace(batch.claims[0], evidence_ids=(ref.evidence_id,)),),
        )
        engine.ingest(p, changed, source_payload=payload)
        assert store._rows("SELECT COUNT(*) FROM archive_objects")[0][0] == 0
        assert repo.reference(ref.evidence_id) == ref
        with pytest.raises((ValidationError, PersistenceError)), store.transaction():
            repo.append_relationship(
                ClaimRelationship(
                    "pclaim_" + "a" * 64, "pclaim_" + "b" * 64, EvidenceRelation.SUPPORTS, 100
                ),
                DATASET.dataset_id,
            )


def test_linked_raw_metadata_is_immutable_and_orphan_checkpoint_detected():
    from eie.evidence.validation import validate_store

    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        provider = source("ALPHA")
        batch = item(provider, 100)
        receipt = engine.ingest(provider, batch)
        with pytest.raises(PersistenceError), store.transaction():
            store._connection.execute("UPDATE observations SET available=available")
        with store.transaction():
            store._connection.execute("DROP TRIGGER immutable_ce_receipts_delete")
            store._connection.execute("DELETE FROM ce_receipts")
        with pytest.raises(PersistenceError, match="checkpoint"):
            validate_store(repo)
        assert repo.store.get_observation(receipt.raw_observation_id) == batch.raw


def test_raw_metadata_collision_and_repeated_payload_validation():
    from examples.evidence_demo import item_payload

    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        provider = source("ALPHA")
        batch = item(provider, 100)
        receipt = engine.ingest(provider, batch, source_payload=item_payload(batch))
        with pytest.raises(ValidationError, match="content hash"):
            engine.ingest(provider, batch, source_payload=b"wrong body")
        modified = replace(batch, raw=replace(batch.raw, source_reference="changed"))
        assert modified.raw.observation_id == batch.raw.observation_id
        with pytest.raises(PersistenceError, match="metadata changed"):
            engine.ingest(provider, modified)
        assert repo.receipt(receipt.receipt_id) == receipt
        assert store._rows("SELECT COUNT(*) FROM ce_receipts")[0][0] == 1


def test_pure_resolution_rejects_ambiguous_revision_inputs_in_any_order():
    from eie.evidence.history import knowledge_intervals
    from eie.evidence.resolution import current_observations, resolve_claim

    with SQLiteEventStore(":memory:") as store:
        repo, engine, _ = setup(store)
        provider = source("ALPHA")
        batch = item(provider, 100)
        receipt = engine.ingest(provider, batch)
        original = repo.assertion(receipt.assertion_ids[0])
        conflict = replace(original, value=replace(TIME, timestamp_ns=TIME.timestamp_ns + 1))
        for rows in ((original, conflict), (conflict, original)):
            with pytest.raises(ValidationError, match="conflicting provider claim"):
                current_observations(rows, 100, None)
            with pytest.raises(ValidationError):
                resolve_claim(CLAIM, DATASET.dataset_id, rows, (), 100)
            with pytest.raises(ValidationError):
                knowledge_intervals(rows, 100)
        single = resolve_claim(CLAIM, DATASET.dataset_id, (original,), (), 100)
        assert resolve_claim(CLAIM, DATASET.dataset_id, (original, original), (), 100) == single
        assert len(knowledge_intervals((original, original), 100)) == 1
        descriptor = batch.descriptors[0]
        conflict_descriptor = replace(
            descriptor, time=replace(TIME, timestamp_ns=TIME.timestamp_ns + 1)
        )
        for rows in ((descriptor, conflict_descriptor), (conflict_descriptor, descriptor)):
            with pytest.raises(ValidationError, match="conflicting provider event"):
                resolve_events(rows, DATASET.dataset_id, 100)
        assert resolve_events((descriptor, descriptor), DATASET.dataset_id, 100) == resolve_events(
            (descriptor,), DATASET.dataset_id, 100
        )
        # Invisible inputs cannot leak the existence of future ambiguity.
        future = replace(conflict, known_from_ns=101)
        assert resolve_claim(CLAIM, DATASET.dataset_id, (original, future), (), 100) == single
        future_descriptor = replace(conflict_descriptor, known_from_ns=101)
        assert resolve_events(
            (descriptor, future_descriptor), DATASET.dataset_id, 100
        ) == resolve_events((descriptor,), DATASET.dataset_id, 100)


def test_identical_append_is_idempotent_before_sealing():
    with SQLiteEventStore(":memory:") as store:
        repo, engine, queries = setup(store)
        provider = source("ALPHA")
        batch = item(provider, 100)
        receipt = engine.ingest(provider, batch)
        with store.transaction():
            repo.append_assertion(repo.assertion(receipt.assertion_ids[0]))
            repo.append_descriptor(batch.descriptors[0])
        assert len(queries.claim_history(CLAIM.claim_id, 100)) == 1
        assert receipt.result_fingerprint and batch.fingerprint


@pytest.mark.parametrize("seed", ("0", "91", "random"))
def test_independent_hash_seed_rebuild(seed):
    import os
    import subprocess
    import sys

    environment = {
        **os.environ,
        "PYTHONHASHSEED": seed,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "src:.",
    }
    actual = subprocess.check_output(
        [sys.executable, "examples/evidence_demo.py"], env=environment, text=True
    )
    expected = subprocess.check_output(
        [sys.executable, "examples/evidence_demo.py"],
        env={**environment, "PYTHONHASHSEED": "17"},
        text=True,
    )
    assert actual == expected


def test_filing_provenance_does_not_make_agreeing_metadata_conflict():
    from eie.evidence.resolution import compatible
    from tests.test_sec_ingestion import CIK_A, T2, URL, fixture, service

    ingestor = service()
    ingestor.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    filing = ingestor.filings_for_cik(CIK_A, T2).filings[0].metadata
    claim = Claim(
        ClaimType.FILING_METADATA,
        ClaimSubject(SubjectKind.FILING, filing.filing_id),
        Predicate.FILING,
    )
    claim.validate_value(filing)
    mirror = replace(
        filing,
        source_observation_id="obs_" + "b" * 64,
        available_from_ns=T2 + 1,
        source_reference=filing.source_reference + "?mirror=1",
    )
    assert compatible(filing, mirror)
    assert not compatible(filing, replace(mirror, report_period="2026-01-01"))


def test_relationship_exposure_preserves_agreement_across_source_references():
    from eie.evidence.claims import RelationshipValue
    from eie.evidence.resolution import compatible
    from eie.graph.exposure import ExposureMeasurement, ExposureType
    from eie.graph.taxonomy import RelationshipType

    a = ClaimSubject(SubjectKind.ENTITY, ISSUER)
    b = ClaimSubject(SubjectKind.ENTITY, "iss_" + "b" * 64)
    exposure = ExposureMeasurement(ExposureType.REVENUE_SHARE, 100, "provider-a", point=0.2)
    left = RelationshipValue(RelationshipType.SUPPLIER_TO, a, b, exposure)
    right = replace(left, exposure=replace(exposure, source_reference="provider-b"))
    assert compatible(left, right)
    assert not compatible(left, replace(right, exposure=replace(right.exposure, point=0.3)))
    assert compatible(replace(left, exposure=None), replace(right, exposure=None))
