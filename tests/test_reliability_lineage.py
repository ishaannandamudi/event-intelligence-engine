from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import JobStatus
from eie.datasets.repository import DatasetRepository
from eie.evidence.claims import Claim, ClaimSubject, OccurrenceValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.references import EvidenceDataset, EvidenceLocator, LocatorKind
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimType, Predicate, SubjectKind
from eie.ingestion.models import RetentionPermission
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput, EvidenceBatch, EvidenceProvider
from eie.providers.evidence.normalization import field_reference, historical_lineage
from eie.reliability.models import ReliabilityQuery, SourceReliabilityPolicy
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import LatencyQuality, ReliabilityQuality
from eie.reliability.validation import validate
from eie.runtime.clock import ManualClock
from examples.historical_rebuild import fixture


def test_real_eie7_acquisition_cannot_enter_earlier_local_baseline(tmp_path):
    job, source, processor = fixture()
    with (
        SQLiteEventStore(tmp_path / "history.sqlite") as store,
        FileArchive(tmp_path / "archive") as archive,
    ):
        historical = DatasetRepository(store)
        assert (
            BackfillCoordinator(
                historical, archive, ((source, processor),), clock=ManualClock(1000)
            ).run(job)
            is JobStatus.COMPLETED
        )
        fact = historical.facts(job.build_id)[0]
        dataset = EvidenceDataset(
            "reliability-historical",
            historical.manifest(job.build_id).content_fingerprint,
            job.build_id,
        )
        p = EvidenceProvider(
            fact.raw.source,
            "BLS-ORIGIN",
            dataset,
            frozenset({ClaimType.EVENT_OCCURRENCE}),
            fact.parser_version,
            normalized_retention=RetentionPermission.ALLOWED,
        )
        acquired = next(
            a
            for a in historical.acquisitions(job.build_id)
            if a.acquisition_id
            in {
                link.acquisition_id
                for link in historical.lineage(job.build_id)
                if link.fact_id == fact.fact_id
            }
        )
        lineage = historical_lineage(fact, job.build_id, "a" * 64, acquired)
        ref = field_reference(
            fact.raw.observation_id,
            fact.raw.source_item_id,
            fact.raw.payload_hash,
            fact.parser_version,
            fact.adapter_version,
            EvidenceLocator(LocatorKind.FIELD, "occurred"),
            True,
        )
        claim = Claim(
            ClaimType.EVENT_OCCURRENCE,
            ClaimSubject(SubjectKind.EVENT, fact.record.event_id),
            Predicate.OCCURRED,
        )
        item = EvidenceBatch(
            fact.resource_id,
            fact.raw,
            lineage,
            (ref,),
            (
                ClaimInput(
                    "occurred",
                    claim,
                    OccurrenceValue(True),
                    (ref.evidence_id,),
                    event_id=fact.record.event_id,
                ),
            ),
            (fact.record,),
        )
        receipt = EvidenceEngine(EvidenceRepository(store)).ingest(p, item)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        # An earlier empty audit must not reject a later, honestly acquired backfill.
        assert not engine.audit(dataset.dataset_id, claim.claim_id, 500, 0).entries
        row = repo.index_claim(receipt.assertion_ids[0])
        assert row.known_ns < 1000 and row.metric_available_ns == 1000
        assert row.clocks.observed.timestamp_ns == 1000
        query = ReliabilityQuery(
            dataset.dataset_id, p.source.name, item.resource_id, 500, SourceReliabilityPolicy()
        )
        assert engine.source_reliability(query).sample_count == 0
        from dataclasses import replace

        current = engine.source_reliability(replace(query, known_as_of_ns=1001))
        assert current.sample_count == 1
        assert ReliabilityQuality.RECONSTRUCTION_LIMITATION in current.quality
        latency = engine.latency(row.artifact_id, 1001)
        assert LatencyQuality.RECONSTRUCTED_AVAILABILITY in latency.quality
        validate(repo, current, deep=True)
