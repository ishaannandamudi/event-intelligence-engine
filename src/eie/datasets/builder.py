"""Deterministic content digests and one-way dataset finalization."""

from collections import Counter
from hashlib import sha256

from eie.backfill.models import CoverageClaim, GapReason, GapRecord, JobStatus, PartitionStatus
from eie.backfill.planner import plan, split
from eie.core.errors import PersistenceError
from eie.core.fingerprints import fingerprint
from eie.datasets.models import BackfillJob, CompletenessReport, DatasetManifest
from eie.datasets.repository import DatasetRepository
from eie.persistence import codec


def records_digest(repository: DatasetRepository, build_id: str) -> str:
    digest = sha256(b"eie:dataset-record-stream:v1\x00")
    for fact in repository.iter_facts(build_id):
        digest.update(bytes.fromhex(fact.content_fingerprint))
    return digest.hexdigest()


def finalize(
    repository: DatasetRepository,
    job: BackfillJob,
    completed_ns: int,
    *,
    expected_content_fingerprint: str | None = None,
) -> DatasetManifest:
    with repository.store.transaction():
        existing = repository.manifest(job.build_id)
        if existing is not None:
            return existing
        parts = repository.partitions(job.build_id)
        by_id = {p.partition_id: (p, progress) for p, progress in parts}
        if any(p.partition_id not in by_id for p in plan(job)):
            raise PersistenceError("dataset has missing planned partitions")
        if any(
            progress.status
            not in (
                PartitionStatus.COMPLETED,
                PartitionStatus.KNOWN_EMPTY,
                PartitionStatus.GAPPED,
                PartitionStatus.SPLIT,
            )
            for _, progress in parts
        ):
            raise PersistenceError("cannot finalize incomplete historical partitions")
        roots = {p.partition_id for p in plan(job)}
        specs = {s.provider_id: s for s in job.definition.sources}
        existing_gaps = {gap.partition_id for gap in repository.gaps(job.build_id)}
        for part, state in parts:
            if state.content_fingerprint is None or (
                part.parent_id is None and part.partition_id not in roots
            ):
                raise PersistenceError("unverified or unplanned terminal partition")
            if part.parent_id is not None and (
                part.parent_id not in by_id
                or by_id[part.parent_id][1].status is not PartitionStatus.SPLIT
                or part
                not in split(
                    by_id[part.parent_id][0], specs[part.provider_id].capability.max_split_depth
                )
            ):
                raise PersistenceError("invalid split parent or child")
            if state.status is PartitionStatus.SPLIT and any(
                child.partition_id not in by_id
                for child in split(part, specs[part.provider_id].capability.max_split_depth)
            ):
                raise PersistenceError("missing split child prevents dataset finalization")
            if state.status in (PartitionStatus.COMPLETED, PartitionStatus.KNOWN_EMPTY):
                pages = repository.acquisitions(job.build_id, part.partition_id)
                if (
                    not pages
                    or pages[-1].next_cursor is not None
                    or pages[-1].coverage is not CoverageClaim.EXHAUSTIVE
                ):
                    raise PersistenceError("complete partition requires exhausted exhaustive input")
            if state.status is PartitionStatus.GAPPED and part.partition_id not in existing_gaps:
                raise PersistenceError("gapped partition requires a recorded gap")
        counts: Counter[str] = Counter()
        event_ids = set()
        quality: Counter[str] = Counter()
        revisions: dict[tuple[str, str, str, str], list[tuple[int, int | None, str]]] = {}
        for fact in repository.iter_facts(job.build_id):
            counts[type(fact.record).__name__] += 1
            quality[fact.quality.value] += 1
            if type(fact.record).__name__ in (
                "CanonicalEvent",
                "FilingMetadata",
                "CorporateEvent",
                "ScheduleRevision",
                "EstimateObservation",
                "ActualObservation",
                "GuidanceObservation",
            ):
                revisions.setdefault(
                    (
                        fact.provider_id,
                        fact.resource_id,
                        type(fact.record).__name__,
                        fact.logical_id,
                    ),
                    [],
                ).append((fact.source_revision, fact.source_available_ns, fact.fact_id))
            if type(fact.record).__name__ == "CanonicalEvent":
                event_ids.add(fact.logical_id)
        anchors = {(p.provider_id, p.resource_id): p for p, _ in parts}
        for (provider, resource, _, _), chain in revisions.items():
            numbers = sorted({number for number, _, _ in chain})
            ambiguous = numbers != list(range(1, len(numbers) + 1))
            clocks = sorted((n, at) for n, at, _ in chain if at is not None)
            ambiguous |= any(a[1] > b[1] for a, b in zip(clocks, clocks[1:], strict=False))
            ambiguous |= len(numbers) != len(chain)
            if ambiguous:
                part = anchors[(provider, resource)]
                repository.add_gap(
                    job.build_id,
                    GapRecord(
                        part.partition_id,
                        provider,
                        resource,
                        job.definition.interval,
                        GapReason.TEMPORAL_AMBIGUITY,
                        completed_ns,
                        evidence=("SOURCE_REVISION_CHAIN_UNPROVEN",),
                    ),
                )
        gaps = repository.gaps(job.build_id)
        counts["CanonicalEventRevisions"] = counts.pop("CanonicalEvent", 0)
        counts["CanonicalEvents"] = len(event_ids)
        counts["RawSourceObservations"] = int(
            str(
                repository.store._rows(
                    "SELECT COUNT(*) FROM historical_observations WHERE build_id=?", (job.build_id,)
                )[0][0]
            )
        )
        acquisitions = repository.acquisitions(job.build_id) + repository.failed_acquisitions(
            job.build_id
        )
        hashes = tuple(sorted({a.object.sha256 for a in acquisitions}))
        counts["ArchiveObjects"] = len(hashes)
        counts["DuplicatesSuppressed"] = sum(p.duplicates for _, p in parts)
        counts["Gaps"] = len(gaps)
        counts["Partitions"] = len(parts)
        leaves = [p for _, p in parts if p.status is not PartitionStatus.SPLIT]
        statuses = Counter(p.status for p in leaves)
        requested_by_resource = Counter(
            (p.provider_id, p.resource_id)
            for p, state in parts
            if state.status is not PartitionStatus.SPLIT
        )
        complete_by_resource = Counter(
            (p.provider_id, p.resource_id)
            for p, state in parts
            if state.status in (PartitionStatus.COMPLETED, PartitionStatus.KNOWN_EMPTY)
        )
        resources = tuple(
            (
                s.provider_id,
                resource,
                requested_by_resource[(s.provider_id, resource)],
                complete_by_resource[(s.provider_id, resource)],
            )
            for s in job.definition.sources
            for resource in s.resources
        )
        completeness = CompletenessReport(
            len(leaves),
            statuses[PartitionStatus.COMPLETED],
            statuses[PartitionStatus.KNOWN_EMPTY],
            statuses[PartitionStatus.FAILED],
            statuses[PartitionStatus.GAPPED],
            statuses[PartitionStatus.PENDING] + statuses[PartitionStatus.RUNNING],
            len(parts) - len(leaves),
            resources,
            tuple(sorted(Counter(g.reason.value for g in gaps).items())),
            tuple(sorted(quality.items())),
        )
        status = JobStatus.COMPLETED_WITH_GAPS if gaps else JobStatus.COMPLETED
        partitions_fingerprint = fingerprint(
            "eie:dataset-partition-results:v1",
            tuple(
                sorted(
                    (
                        (
                            p.provider_id,
                            p.resource_id,
                            p.interval,
                            p.depth,
                            progress.status,
                            progress.content_fingerprint,
                        )
                        for p, progress in parts
                    ),
                    key=lambda item: fingerprint("partition-order", item),
                )
            ),
        )
        manifest = DatasetManifest(
            job,
            status,
            tuple(p for _, p in parts),
            gaps,
            hashes,
            tuple(sorted(counts.items())),
            records_digest(repository, job.build_id),
            partitions_fingerprint,
            completeness,
            completed_ns,
            repository.store.schema_version,
        )
        if (
            expected_content_fingerprint is not None
            and manifest.content_fingerprint != expected_content_fingerprint
        ):
            raise PersistenceError("same-version archive rebuild content disagrees with original")
        repository.transition(job.build_id, status)
        repository.store._connection.execute(
            "INSERT INTO dataset_manifests VALUES (?,?)", (job.build_id, codec.dumps(manifest))
        )
        return manifest
