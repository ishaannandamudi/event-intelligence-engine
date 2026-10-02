"""Completed EIE-7 builds retain actual acquisition separately from historical truth."""

from dataclasses import replace

import pytest

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import JobStatus
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.datasets.repository import DatasetRepository
from eie.evidence.claims import Claim, ClaimSubject, OccurrenceValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.queries import EvidenceQueries
from eie.evidence.references import (
    AcquisitionLineage,
    EvidenceDataset,
    EvidenceLocator,
    LocatorKind,
)
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimType, Predicate, ResolutionState, SubjectKind
from eie.ingestion.models import RetentionPermission
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput, EvidenceBatch, EvidenceProvider
from eie.providers.evidence.normalization import field_reference, historical_lineage
from eie.runtime.clock import ManualClock
from examples.historical_rebuild import fixture


def test_historical_build_lineage_live_coexistence_and_no_fabricated_live_clock(tmp_path):
    job, source, processor = fixture()
    with (
        SQLiteEventStore(tmp_path / "lineage.sqlite") as store,
        FileArchive(tmp_path / "archive") as archive,
    ):
        historical = DatasetRepository(store)
        coordinator = BackfillCoordinator(
            historical, archive, ((source, processor),), clock=ManualClock(1000)
        )
        assert coordinator.run(job) is JobStatus.COMPLETED
        manifest = historical.manifest(job.build_id)
        fact = historical.facts(job.build_id)[0]
        dataset = EvidenceDataset("historical", manifest.content_fingerprint, job.build_id)
        provider = EvidenceProvider(
            fact.raw.source,
            "BLS-ORIGIN",
            dataset,
            frozenset({ClaimType.EVENT_OCCURRENCE}),
            fact.parser_version,
            normalized_retention=RetentionPermission.ALLOWED,
        )
        lineage = historical_lineage(
            fact,
            job.build_id,
            fingerprint("eie:settings-test:v1", "historical"),
            next(
                a
                for a in historical.acquisitions(job.build_id)
                if a.acquisition_id
                in {
                    link.acquisition_id
                    for link in historical.lineage(job.build_id)
                    if link.fact_id == fact.fact_id
                }
            ),
        )
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
        batch = EvidenceBatch(
            fact.resource_id,
            fact.raw,
            lineage,
            (ref,),
            (
                ClaimInput(
                    "occurrence",
                    claim,
                    OccurrenceValue(True),
                    (ref.evidence_id,),
                    event_id=fact.record.event_id,
                ),
            ),
            (fact.record,),
        )
        repo = EvidenceRepository(store)
        engine = EvidenceEngine(repo)
        receipt = engine.ingest(provider, batch)
        q = EvidenceQueries(repo, dataset.dataset_id)
        assert q.resolution(claim.claim_id, lineage.known_from_ns).value == OccurrenceValue(True)
        assert (
            q.resolution(claim.claim_id, lineage.known_from_ns - 1).state is ResolutionState.ABSENT
        )
        assert repo.lineage(receipt.lineage_id).ingested_at_ns == 1000
        assert repo.lineage(receipt.lineage_id).known_from_ns < 1000
        assert repo.receipt(receipt.receipt_id) == receipt
        with pytest.raises(ValidationError), store.transaction():
            repo.append_dataset(replace(dataset, fingerprint="b" * 64))
        with pytest.raises(ValidationError), store.transaction():
            repo.append_lineage(replace(lineage, historical_fact_id="missing"), dataset.dataset_id)
        with pytest.raises(ValidationError), store.transaction():
            repo.append_lineage(
                replace(lineage, historical_acquisition_id="missing"), dataset.dataset_id
            )
        with pytest.raises(ValidationError), store.transaction():
            repo.append_lineage(
                replace(lineage, source_available_ns=lineage.source_available_ns + 1),
                dataset.dataset_id,
            )
        live_dataset = EvidenceDataset("live", fingerprint("eie:live-dataset:v1", "fixture"))
        live_provider = replace(provider, dataset=live_dataset)
        live_raw = replace(fact.raw, source_observed_at_ns=2000, ingested_at_ns=2000)
        live_lineage = AcquisitionLineage(
            live_raw.observation_id, 2000, 2000, fact.parser_version, "a" * 64
        )
        live_batch = replace(batch, raw=live_raw, lineage=live_lineage, events=())
        live_receipt = engine.ingest(live_provider, live_batch)
        assert live_raw.observation_id == fact.raw.observation_id
        assert store._rows("SELECT COUNT(*) FROM observations")[0][0] == 1
        assert store._rows("SELECT COUNT(*) FROM ce_references")[0][0] == 1
        assert receipt.lineage_id != live_receipt.lineage_id
        assert (
            EvidenceQueries(repo, live_dataset.dataset_id).resolution(claim.claim_id, 1999).state
            is ResolutionState.ABSENT
        )
        assert (
            EvidenceQueries(repo, live_dataset.dataset_id)
            .resolution(claim.claim_id, 2000)
            .summary.independent_source_count
            == 1
        )


def test_missing_historical_build_fails_before_artifacts():
    with SQLiteEventStore(":memory:") as store:
        repo = EvidenceRepository(store)
        with pytest.raises(ValidationError), store.transaction():
            repo.append_dataset(EvidenceDataset("unknown", "a" * 64, "missing"))
        assert store._rows("SELECT COUNT(*) FROM ce_datasets")[0][0] == 0
