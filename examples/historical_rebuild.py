"""Offline partial build, restart, content-addressed archive and deterministic rebuild."""

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

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
from eie.backfill.rebuild import rebuild
from eie.backfill.sources import ContractHistoryProcessor, FixtureHistoricalSource, HistoricalPage
from eie.core.quality import ConfirmationState
from eie.core.serialization import canonical_json_bytes
from eie.datasets.models import BackfillJob, DatasetDefinition, HistoricalInput, ValidationStatus
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.datasets.validation import validate_dataset
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import MACRO_CPI_US
from eie.ingestion.models import RetentionMode, RetentionPermission, RetentionPolicy
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
from eie.sources.models import SourceIdentity
from eie.sources.trust import SourceClass


def item(
    at: int, key: str, *, source_revision: int = 1, title: str = "Synthetic history"
) -> HistoricalInput:
    event = CanonicalEvent(
        EventIdentity(MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), key),
        EventRevision(
            source_revision,
            RevisionKind.ORIGINAL if source_revision == 1 else RevisionKind.UPDATE,
            at,
            None if source_revision == 1 else source_revision - 1,
        ),
        LifecycleState.RELEASED,
        ConfirmationState.UNVERIFIED,
        ("obs_" + "a" * 64,),
        frozenset({SourceClass.UNVERIFIED}),
        title,
        effective_at_ns=at,
    )
    source = SourceIdentity(
        "SYNTH_HIST", "synthetic-history-1", "historical-contract-1", SourceClass.UNVERIFIED
    )
    return HistoricalInput(
        source,
        key,
        str(source_revision),
        sha256(canonical_json_bytes(event)).hexdigest(),
        at,
        at,
        ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME,
        source_revision,
        event,
    )


def fixture() -> tuple[BackfillJob, FixtureHistoricalSource, ContractHistoryProcessor]:
    processor = ContractHistoryProcessor()
    spec = SourceSpec(
        "SYNTH_HIST",
        ("macro",),
        processor.adapter_version,
        processor.parser_version,
        HistoricalCapability(max_range_ns=5, assumptions=("SYNTHETIC_FIXTURE_ONLY",)),
    )
    definition = DatasetDefinition(
        "synthetic-history",
        TimeRange(10, 20),
        (spec,),
        partition_policy=PartitionPolicy(5),
        archive_policy=RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED),
    )
    pages = {
        ("macro", start, start + 5, None): HistoricalPage(
            codec.dumps((item(start + 2, f"cpi-{start}"),)),
            "https://example.invalid/history",
            coverage=CoverageClaim.EXHAUSTIVE,
        )
        for start in (10, 15)
    }
    return BackfillJob(definition), FixtureHistoricalSource(spec, pages), processor


def demo(root: Path) -> dict[str, object]:
    job, source, processor = fixture()
    clock = ManualClock(1000)
    with FileArchive(root / "archive") as archive:
        with SQLiteEventStore(root / "events.sqlite") as store:
            repository = DatasetRepository(store)
            runtime = BackfillCoordinator(repository, archive, ((source, processor),), clock=clock)
            assert runtime.run(job, max_pages=1) is JobStatus.PAUSED
        with SQLiteEventStore(root / "events.sqlite") as store:
            repository = DatasetRepository(store)
            runtime = BackfillCoordinator(repository, archive, ((source, processor),), clock=clock)
            assert runtime.run(job) is JobStatus.COMPLETED
            manifest = repository.manifest(job.build_id)
            assert manifest is not None
            assert (
                validate_dataset(repository, archive, job.build_id).status is ValidationStatus.VALID
            )
            reader = DatasetReader(repository, job.build_id)
            assert len(reader.events_known_at(14).events) == 1
            assert len(reader.events_known_at(19).events) == 2
            rebuilt = rebuild(
                repository,
                archive,
                job.build_id,
                {source.spec.provider_id: processor},
                "archive-rebuild",
                clock=clock,
            )
            second = repository.manifest(rebuilt.build_id)
            assert second is not None and second.content_fingerprint == manifest.content_fingerprint
            assert (
                validate_dataset(repository, archive, rebuilt.build_id).status
                is ValidationStatus.VALID
            )
            return {
                "partitions": len(manifest.partitions),
                "events": 2,
                "content_fingerprint": manifest.content_fingerprint,
                "rebuild_matches": True,
            }


if __name__ == "__main__":
    import json

    with TemporaryDirectory() as directory:
        print(json.dumps(demo(Path(directory)), sort_keys=True))
