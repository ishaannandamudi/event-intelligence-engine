"""Real completed EIE-7 lineage cannot backdate the availability of NLP processing."""

from dataclasses import replace
from hashlib import sha256

import pytest
from nlp_helpers import BODY, CATALOG, ISSUER, model

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import (
    CoverageClaim,
    HistoricalCapability,
    JobStatus,
    PartitionPolicy,
    ReconstructionQuality,
    SourceSpec,
    TimeRange,
)
from eie.backfill.sources import FixtureHistoricalSource, HistoricalPage
from eie.core.errors import ValidationError
from eie.core.quality import ConfirmationState
from eie.datasets.models import BackfillJob, DatasetDefinition, HistoricalFact
from eie.datasets.repository import DatasetRepository
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.queries import EvidenceQueries
from eie.evidence.references import EvidenceDataset
from eie.evidence.schema import ClaimType, ResolutionState
from eie.ingestion.models import RetentionMode, RetentionPermission, RetentionPolicy
from eie.nlp.engine import TextEngine
from eie.nlp.queries import TextQueries
from eie.nlp.repository import TextRepository
from eie.nlp.validation import validate_store
from eie.nlp.validation_models import ValidationStatus
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import EvidenceProvider
from eie.providers.evidence.normalization import historical_lineage
from eie.providers.text.adapter import document
from eie.providers.text.models import TextProvider
from eie.runtime.clock import ManualClock
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass


class HistoricalTextFixture:
    adapter_version = "text-fixture-1"
    parser_version = "text-acquisition-1"

    def normalize(self, page, partition, acquired_ns, settings):
        source = SourceIdentity(
            partition.provider_id, "1", self.adapter_version, SourceClass.UNVERIFIED
        )
        raw = RawSourceObservation(
            source,
            "archived-article",
            "1",
            sha256(page.body).hexdigest(),
            "text/plain",
            acquired_ns,
            acquired_ns,
            published_at_ns=12,
        )
        event = CanonicalEvent(
            EventIdentity(
                EventType(EventFamily.OTHER, "OTHER.UNCLASSIFIED"),
                EventScope(ScopeKind.COMPANY, (ISSUER,)),
                "archived-text-envelope",
            ),
            EventRevision(1, RevisionKind.ORIGINAL, acquired_ns),
            LifecycleState.RELEASED,
            ConfirmationState.UNVERIFIED,
            (raw.observation_id,),
            frozenset({SourceClass.UNVERIFIED}),
            "Archived text envelope",
            published_at_ns=12,
        )
        return (
            HistoricalFact(
                partition.provider_id,
                partition.resource_id,
                event.event_id,
                1,
                12,
                ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME,
                raw,
                event,
                self.adapter_version,
                self.parser_version,
            ),
        )


def test_completed_archive_build_processing_availability_and_pinned_replay(tmp_path):
    processor = HistoricalTextFixture()
    spec = SourceSpec(
        "TEXT_ARCHIVE",
        ("documents",),
        processor.adapter_version,
        processor.parser_version,
        HistoricalCapability(max_range_ns=10),
    )
    policy = RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED)
    job = BackfillJob(
        DatasetDefinition(
            "historical-text",
            TimeRange(10, 20),
            (spec,),
            partition_policy=PartitionPolicy(10),
            archive_policy=policy,
        )
    )
    payload = BODY.encode()
    source = FixtureHistoricalSource(
        spec,
        {
            ("documents", 10, 20, None): HistoricalPage(
                payload,
                "https://example.invalid/archived-article",
                content_type="text/plain",
                coverage=CoverageClaim.EXHAUSTIVE,
            )
        },
    )
    with (
        SQLiteEventStore(tmp_path / "historical.sqlite") as store,
        FileArchive(tmp_path / "archive") as archive,
    ):
        history = DatasetRepository(store)
        assert (
            BackfillCoordinator(
                history, archive, ((source, processor),), clock=ManualClock(1000)
            ).run(job)
            is JobStatus.COMPLETED
        )
        fact = history.facts(job.build_id)[0]
        acquired = history.acquisitions(job.build_id)[0]
        dataset = EvidenceDataset(
            "historical-text", history.manifest(job.build_id).content_fingerprint, job.build_id
        )
        provider = TextProvider(
            EvidenceProvider(
                fact.raw.source,
                "ARCHIVE_ORIGIN",
                dataset,
                frozenset(ClaimType),
                processor.parser_version,
                RetentionPermission.ALLOWED,
                policy,
            ),
            currency="USD",
        )
        lineage = historical_lineage(fact, job.build_id, "a" * 64, acquired)
        doc = document(provider, fact.raw, lineage)
        repo = TextRepository(store)
        engine = TextEngine(repo)
        queries = TextQueries(repo, dataset.dataset_id)
        run = engine.ingest(provider, doc, payload, CATALOG, 1200)
        assert run.accepted and lineage.known_from_ns == 12 and doc.raw.ingested_at_ns == 1000
        assert not queries.extractions_as_of(1199)
        claim = run.accepted[0].claim_ids[0]
        q = EvidenceQueries(repo.evidence, dataset.dataset_id)
        assert q.resolution(claim, 12).state is ResolutionState.ABSENT
        assert q.resolution(claim, 1199).state is ResolutionState.ABSENT
        assert q.resolution(claim, 1200).value is not None
        assert all(
            repo.evidence.assertion(i).known_from_ns == 1200
            for link in run.accepted
            for i in link.assertion_ids
        )
        before = dumps(queries.audit(run.run_id, 1200))
        reprocessed = engine.ingest(
            provider,
            doc,
            payload,
            CATALOG,
            1600,
            model=model(version="later"),
            previous_run_id=run.run_id,
        )
        assert reprocessed.run_id != run.run_id and dumps(queries.audit(run.run_id, 1200)) == before
        assert validate_store(repo).status is ValidationStatus.VALID
        bad_doc = replace(doc, source_lineage=replace(lineage, historical_acquisition_id="wrong"))
        with pytest.raises(ValidationError):
            engine.ingest(provider, bad_doc, payload, CATALOG, 1800)
        assert len(queries.extractions_as_of(2000)) == 2
