"""Independent integrity, indexed contracts and frozen lineage checks without refetch."""

import sqlite3
from collections import Counter

from eie.archive.models import ArchiveObject
from eie.archive.storage import FileArchive
from eie.backfill.coordinator import partition_content
from eie.backfill.models import PartitionStatus
from eie.backfill.planner import plan, split
from eie.core.errors import EIEError
from eie.core.fingerprints import fingerprint
from eie.datasets.builder import records_digest
from eie.datasets.models import DatasetValidationResult, ValidationStatus
from eie.datasets.repository import DatasetRepository, typed
from eie.ingestion.models import RetentionMode
from eie.sources.models import RawSourceObservation


def validate_dataset(
    repository: DatasetRepository, archive: FileArchive, build_id: str
) -> DatasetValidationResult:
    try:
        return _validate(repository, archive, build_id)
    except (EIEError, sqlite3.Error, ValueError, KeyError):
        return DatasetValidationResult(
            build_id, ValidationStatus.INVALID, ("INVALID_STORED_CONTRACT",), ""
        )


def _validate(
    repository: DatasetRepository, archive: FileArchive, build_id: str
) -> DatasetValidationResult:
    manifest = repository.manifest(build_id)
    if manifest is None:
        return DatasetValidationResult(
            build_id, ValidationStatus.INVALID, ("MISSING_MANIFEST",), ""
        )
    errors: list[str] = []
    parts = repository.partitions(build_id)
    by_id = {p.partition_id: (p, state) for p, state in parts}
    roots = {p.partition_id for p in plan(manifest.job)}
    if not roots <= set(by_id):
        errors.append("MISSING_PARTITION")
    if tuple(progress for _, progress in parts) != manifest.partitions:
        errors.append("PARTITION_MANIFEST_MISMATCH")
    specs = {s.provider_id: s for s in manifest.job.definition.sources}
    all_inputs = repository.acquisitions(build_id) + repository.failed_acquisitions(build_id)
    if tuple(sorted({a.object.sha256 for a in all_inputs})) != manifest.archive_hashes:
        errors.append("ARCHIVE_MANIFEST_MISMATCH")
    for part, progress in parts:
        spec = specs[part.provider_id]
        if part.job_id != manifest.job.job_id or part.resource_id not in spec.resources:
            errors.append("INVALID_PARTITION_CONTEXT")
        if part.parent_id is None:
            if part.partition_id not in roots:
                errors.append("UNPLANNED_PARTITION")
        elif part.parent_id not in by_id or part not in split(
            by_id[part.parent_id][0], spec.capability.max_split_depth
        ):
            errors.append("INVALID_SPLIT_LINEAGE")
        if progress.status not in (
            PartitionStatus.COMPLETED,
            PartitionStatus.KNOWN_EMPTY,
            PartitionStatus.GAPPED,
            PartitionStatus.SPLIT,
        ):
            errors.append("INCOMPLETE_PARTITION")
        if progress.status is PartitionStatus.SPLIT and any(
            child.partition_id not in by_id
            for child in split(part, spec.capability.max_split_depth)
        ):
            errors.append("MISSING_SPLIT_CHILD")
        acquisitions = repository.acquisitions(build_id, part.partition_id)
        if tuple(a.page_number for a in acquisitions) != tuple(range(1, progress.pages + 1)):
            errors.append("INVALID_PAGE_CHAIN")
        if progress.content_fingerprint != partition_content(repository, build_id, part):
            errors.append("INVALID_PARTITION_CONTENT")
        cursors = tuple(a.next_cursor for a in acquisitions[:-1])
        if progress.visited_cursors != cursors or (
            acquisitions and acquisitions[-1].next_cursor != progress.cursor
        ):
            errors.append("INVALID_CURSOR_CHAIN")
        if (
            progress.status in (PartitionStatus.COMPLETED, PartitionStatus.KNOWN_EMPTY)
            and acquisitions
            and acquisitions[-1].next_cursor is not None
        ):
            errors.append("INCOMPLETE_PAGE_CHAIN")
        for acquisition in acquisitions + repository.failed_acquisitions(
            build_id, part.partition_id
        ):
            if (
                acquisition.build_id,
                acquisition.partition_id,
                acquisition.provider_id,
                acquisition.resource_id,
                acquisition.adapter_version,
                acquisition.parser_version,
                acquisition.retention,
            ) != (
                build_id,
                part.partition_id,
                part.provider_id,
                part.resource_id,
                spec.adapter_version,
                spec.parser_version,
                manifest.job.definition.archive_policy,
            ):
                errors.append("INVALID_ACQUISITION_CONTEXT")
            objects = repository.store._rows(
                "SELECT payload FROM archive_objects WHERE hash=?", (acquisition.object.sha256,)
            )
            if not objects or typed(objects[0][0], ArchiveObject) != acquisition.object:
                errors.append("INVALID_ARCHIVE_METADATA")
            if acquisition.retention.mode is RetentionMode.BOUNDED_RAW:
                try:
                    archive.read(acquisition.object)
                except EIEError:
                    errors.append("ARCHIVE_INTEGRITY_FAILURE")
    facts = {f.fact_id: f for f in repository.iter_facts(build_id)}
    linked = set()
    acquisitions_by_id = {a.acquisition_id: a for a in repository.acquisitions(build_id)}
    for lineage in repository.lineage(build_id):
        fact = facts.get(lineage.fact_id)
        linked_acquisition = acquisitions_by_id.get(lineage.acquisition_id)
        if fact is None or linked_acquisition is None or lineage.partition_id not in by_id:
            errors.append("INVALID_LINEAGE_REFERENCE")
            continue
        if (
            lineage.observation_id,
            lineage.object_hash,
            lineage.adapter_version,
            lineage.parser_version,
            lineage.settings_fingerprint,
            lineage.partition_id,
            lineage.build_id,
        ) != (
            fact.raw.observation_id,
            linked_acquisition.object.sha256,
            fact.adapter_version,
            fact.parser_version,
            manifest.job.definition.settings.fingerprint,
            linked_acquisition.partition_id,
            build_id,
        ):
            errors.append("INVALID_LINEAGE_CONTEXT")
        linked.add(fact.fact_id)
    if linked != set(facts):
        errors.append("ORPHAN_NORMALIZED_RECORD")
    for row in repository.store._rows(
        "SELECT id,provider,resource,logical_id,revision,available,kind,observation_id "
        "FROM historical_facts WHERE build_id=?",
        (build_id,),
    ):
        f = facts.get(str(row[0]))
        if f is None or tuple(row[1:]) != (
            f.provider_id,
            f.resource_id,
            f.logical_id,
            f.source_revision,
            f.source_available_ns,
            type(f.record).__name__,
            f.raw.observation_id,
        ):
            errors.append("INVALID_FACT_INDEX")
    for row in repository.store._rows(
        "SELECT f.id,o.payload FROM historical_facts f JOIN historical_observations o "
        "ON f.build_id=o.build_id AND f.observation_id=o.id WHERE f.build_id=?",
        (build_id,),
    ):
        fact = facts.get(str(row[0]))
        if fact is None or typed(row[1], RawSourceObservation) != fact.raw:
            errors.append("INVALID_OBSERVATION_LINEAGE")
    for f in facts.values():
        if (f.adapter_version, f.parser_version, f.raw.source.source_class) != (
            specs[f.provider_id].adapter_version,
            specs[f.provider_id].parser_version,
            specs[f.provider_id].source_class,
        ):
            errors.append("INVALID_FACT_CONTEXT")
    counts: Counter[str] = Counter(type(f.record).__name__ for f in facts.values())
    counts["CanonicalEventRevisions"] = counts.pop("CanonicalEvent", 0)
    counts["CanonicalEvents"] = len(
        {f.logical_id for f in facts.values() if type(f.record).__name__ == "CanonicalEvent"}
    )
    counts["RawSourceObservations"] = int(
        str(
            repository.store._rows(
                "SELECT COUNT(*) FROM historical_observations WHERE build_id=?", (build_id,)
            )[0][0]
        )
    )
    counts["ArchiveObjects"] = len({a.object.sha256 for a in all_inputs})
    counts["DuplicatesSuppressed"] = sum(p.duplicates for _, p in parts)
    counts["Gaps"] = len(repository.gaps(build_id))
    counts["Partitions"] = len(parts)
    if tuple(sorted(counts.items())) != manifest.record_counts:
        errors.append("INVALID_MANIFEST_COUNTS")
    if repository.gaps(build_id) != manifest.gaps:
        errors.append("INVALID_MANIFEST_GAPS")
    if records_digest(repository, build_id) != manifest.records_fingerprint:
        errors.append("INVALID_DATASET_CONTENT")
    partition_digest = fingerprint(
        "eie:dataset-partition-results:v1",
        tuple(
            sorted(
                (
                    (
                        p.provider_id,
                        p.resource_id,
                        p.interval,
                        p.depth,
                        state.status,
                        state.content_fingerprint,
                    )
                    for p, state in parts
                ),
                key=lambda item: fingerprint("partition-order", item),
            )
        ),
    )
    if partition_digest != manifest.partitions_fingerprint:
        errors.append("INVALID_PARTITION_MANIFEST_CONTENT")
    leaves = [state for _, state in parts if state.status is not PartitionStatus.SPLIT]
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
    expected_resources = tuple(
        (
            s.provider_id,
            resource,
            requested_by_resource[(s.provider_id, resource)],
            complete_by_resource[(s.provider_id, resource)],
        )
        for s in manifest.job.definition.sources
        for resource in s.resources
    )
    c = manifest.completeness
    if (
        c.requested_partitions != len(leaves)
        or c.resources != expected_resources
        or c.split_partitions != len(parts) - len(leaves)
        or c.failed_partitions != 0
        or c.pending_partitions != 0
        or c.completed_partitions != sum(p.status is PartitionStatus.COMPLETED for p in leaves)
        or c.known_empty_partitions != sum(p.status is PartitionStatus.KNOWN_EMPTY for p in leaves)
        or c.gapped_partitions != sum(p.status is PartitionStatus.GAPPED for p in leaves)
        or c.gap_reasons != tuple(sorted(Counter(g.reason.value for g in manifest.gaps).items()))
        or c.reconstruction_quality
        != tuple(sorted(Counter(f.quality.value for f in facts.values()).items()))
    ):
        errors.append("INVALID_COMPLETENESS_REPORT")
    if manifest.store_schema_version != repository.store.schema_version:
        errors.append("INVALID_STORE_SCHEMA_VERSION")
    if repository.store.validate():
        errors.append("DATABASE_INTEGRITY_FAILURE")
    result = (
        ValidationStatus.INVALID
        if errors
        else ValidationStatus.VALID_WITH_KNOWN_GAPS
        if manifest.gaps
        else ValidationStatus.VALID
    )
    return DatasetValidationResult(
        build_id, result, tuple(sorted(set(errors))), manifest.content_fingerprint
    )
