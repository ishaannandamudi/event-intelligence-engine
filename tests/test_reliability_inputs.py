from dataclasses import replace

from eie.events.revisions import RevisionKind
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.claims import MeasurementSemantics, TimePrecision, TimeValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimStatus
from eie.expectations.observations import ActualRole
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.models import ReliabilityQuery, SourceReliabilityPolicy
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import RevisionBehavior as B
from eie.reliability.schema import SubjectType
from eie.reliability.validation import validate
from examples.intelligence_fixtures import DATASET, DEFAULT_PERIOD, batch, source
from examples.reliability_fixtures import SECOND, ingest


def test_canonical_expected_macro_revision_and_raw_observation_projection():
    with SQLiteEventStore(":memory:") as store:
        p = source("GDP")
        er = EvidenceRepository(store)
        ee = EvidenceEngine(er)
        rr = ReliabilityRepository(store)
        typ = EventType(EventFamily.MACRO, "MACRO.GDP.US")
        first = batch(p, 10 * SECOND, key="GDP", event_type=typ, measurement="MACRO.GDP")
        ee.ingest(p, first)
        original = rr.index_event(
            DATASET.dataset_id,
            first.events[0].event_id,
            10 * SECOND,
            "financial",
            "GDP_ORIGIN",
            first.raw.observation_id,
        )
        revised = batch(
            p,
            20 * SECOND,
            key="GDP",
            event_type=typ,
            measurement="MACRO.GDP",
            prior=first,
            status=ClaimStatus.UPDATED,
        )
        revised = replace(
            revised,
            events=(
                replace(
                    revised.events[0],
                    revision=replace(revised.events[0].revision, kind=RevisionKind.REVISION),
                ),
            ),
        )
        receipt = ee.ingest(p, revised)
        row = rr.index_event(
            DATASET.dataset_id,
            revised.events[0].event_id,
            20 * SECOND,
            "financial",
            "GDP_ORIGIN",
            revised.raw.observation_id,
        )
        assert row.behavior is B.EXPECTED_REVISION
        raw = rr.index_observation(
            DATASET.dataset_id, first.raw.observation_id, "financial", "GDP_ORIGIN"
        )
        assert raw.clocks.normalized is None and raw.clocks.source_available is None
        q = ReliabilityQuery(
            DATASET.dataset_id,
            "GDP",
            "financial",
            30 * SECOND,
            SourceReliabilityPolicy(),
            kind=SubjectType.EVENT,
        )
        result = ReliabilityQueries(rr).source_reliability(q)
        assert next(r for r in result.behavior if r.name == "EXPECTED_REVISION").count == 1
        assert next(r for r in result.behavior if r.name == "ERROR_CORRECTION").count == 0
        validate(rr, result, deep=True)
        assert receipt.assertion_ids and original.number == 1


def test_field_and_event_type_grouping_and_numeric_expected_semantics():
    with SQLiteEventStore(":memory:") as store:
        p = source("A")
        er = EvidenceRepository(store)
        ee = EvidenceEngine(er)
        rr = ReliabilityRepository(store)
        time = batch(
            p, 10 * SECOND, key="time", value=TimeValue(TimePrecision.EXACT, "1970-01-01", 0)
        )
        numeric = batch(
            p,
            20 * SECOND,
            key="numeric",
            semantics=MeasurementSemantics(
                actual_role=ActualRole.REVISED_PREVIOUS, previous_period=DEFAULT_PERIOD
            ),
            event_type=EventType(EventFamily.CORPORATE_ACTION, "CORPORATE_ACTION.MERGER"),
        )
        for item in (time, numeric):
            receipt = ee.ingest(p, item)
            rr.index_claim(receipt.assertion_ids[0])
        engine = ReliabilityQueries(rr)
        query = ReliabilityQuery(
            DATASET.dataset_id, "A", "financial", 30 * SECOND, SourceReliabilityPolicy()
        )
        event_time = engine.source_reliability(
            replace(query, field="EFFECTIVE_AT", event_type="EARNINGS.RELEASE")
        )
        numbers = engine.source_reliability(
            replace(query, field="ACTUAL", event_type="CORPORATE_ACTION.MERGER")
        )
        assert event_time.sample_count == numbers.sample_count == 1
        assert next(r for r in event_time.behavior if r.name == "EXPECTED_REVISION").count == 0
        assert next(r for r in numbers.behavior if r.name == "EXPECTED_REVISION").count == 1
        changed = engine.source_reliability(
            replace(query, policy=replace(query.policy, minimum_count=20, version="2"))
        )
        assert changed.artifact_id != engine.source_reliability(query).artifact_id


def test_source_local_missing_normalization_is_not_ingestion_or_availability():
    with SQLiteEventStore(":memory:") as store:
        p = source("A")
        er = EvidenceRepository(store)
        receipt = EvidenceEngine(er).ingest(p, batch(p, 10 * SECOND))
        rr = ReliabilityRepository(store)
        row = rr.index_claim(receipt.assertion_ids[0])
        assert row.clocks.normalized is None
        assert row.clocks.snapshot is None and row.clocks.publication is None
        result = ReliabilityQueries(rr).latency(row.artifact_id, 20 * SECOND)
        assert all(c.delta is None for c in result.components[5:])


def test_actual_extraction_clocks_and_compatible_coarse_refinement():
    from eie.reliability.reliability import calculate

    with SQLiteEventStore(":memory:") as store:
        _, a = ingest(store, "A", 10 * SECOND)
        _, b = ingest(store, "B", 20 * SECOND)
        assert a.clocks.normalized.timestamp_ns == 10 * SECOND
        qa = replace(a, content_time=TimeValue(TimePrecision.DATE_ONLY, "1970-01-01"))
        qb = replace(
            b,
            content_time=TimeValue(TimePrecision.EXACT, "1970-01-01", SECOND),
            value_fingerprint="b" * 64,
        )
        query = ReliabilityQuery(
            DATASET.dataset_id, "A", "financial", 30 * SECOND, SourceReliabilityPolicy()
        )
        result = calculate(query, (qa,), (qa, qb))
        assert result.corroboration.count == 1 and result.conflict.count == 0


def test_offline_example_preserves_source_class_and_historical_bytes(tmp_path):
    from examples.source_reliability import run

    result = run(tmp_path / "example.sqlite")
    assert result["source_class_A"] == result["source_class_B"] == ("OFFICIAL_PRIMARY",)
    assert result["A_error_corrections"] == 1 and result["B_error_corrections"] == 0
    assert result["historical_bytes_unchanged"] and result["restart_bytes_identical"]


def test_peer_correction_rebinding_retires_old_claim_confirmation():
    from eie.entities.sec import sec_issuer_id
    from eie.evidence.claims import ClaimSubject
    from eie.evidence.schema import SubjectKind
    from eie.providers.sec.models import CIK
    from examples.reliability_fixtures import provider

    with SQLiteEventStore(":memory:") as store:
        ingest(store, "A", 10 * SECOND)
        previous, _ = ingest(store, "B", 20 * SECOND)
        p = provider("B")
        revised = batch(p, 30 * SECOND, key="release", prior=previous, status=ClaimStatus.CORRECTED)
        changed = replace(
            revised.claims[0].claim,
            subject=ClaimSubject(SubjectKind.ENTITY, sec_issuer_id(CIK("0000789019"))),
        )
        revised = replace(revised, claims=(replace(revised.claims[0], claim=changed),), events=())
        receipt = EvidenceEngine(EvidenceRepository(store)).ingest(p, revised)
        repo = ReliabilityRepository(store)
        repo.index_claim(receipt.assertion_ids[0])
        result = ReliabilityQueries(repo).source_reliability(
            ReliabilityQuery(
                DATASET.dataset_id, "A", "financial", 40 * SECOND, SourceReliabilityPolicy()
            )
        )
        assert result.corroboration.count == 0 and result.conflict.count == 0
        validate(repo, result, deep=True)
