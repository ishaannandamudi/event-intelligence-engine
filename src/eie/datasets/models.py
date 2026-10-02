"""Historical facts wrap unchanged domain contracts and honest local raw clocks."""

from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum

from eie.backfill.models import (
    GapRecord,
    JobStatus,
    PartitionPolicy,
    PartitionProgress,
    ProvenanceKind,
    ReconstructionQuality,
    SourceSpec,
    TimeRange,
    sorted_labels,
)
from eie.config.settings import Settings
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.corporate.models import CorporateEvent
from eie.entities.models import Issuer, Listing, Security
from eie.events.models import CanonicalEvent
from eie.events.taxonomy import EventFamily
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.observations import ActualObservation, EstimateObservation
from eie.filings.models import FilingMetadata
from eie.ingestion.models import RetentionPolicy, identifier, label
from eie.scheduled.models import ScheduleRevision
from eie.sources.models import SHA256, RawSourceObservation, SourceIdentity
from eie.version import __version__

DATASET_SCHEMA_VERSION = "1.0"
DomainRecord = (
    CanonicalEvent
    | FilingMetadata
    | CorporateEvent
    | ScheduleRevision
    | EstimateObservation
    | ActualObservation
    | GuidanceObservation
    | Issuer
    | Security
    | Listing
)
RECORD_TYPES = (
    CanonicalEvent,
    FilingMetadata,
    CorporateEvent,
    ScheduleRevision,
    EstimateObservation,
    ActualObservation,
    GuidanceObservation,
    Issuer,
    Security,
    Listing,
)


@dataclass(frozen=True, slots=True)
class DatasetDefinition:
    namespace: str
    interval: TimeRange
    sources: tuple[SourceSpec, ...]
    settings: Settings = Settings()
    partition_policy: PartitionPolicy = PartitionPolicy()
    archive_policy: RetentionPolicy = RetentionPolicy()
    event_families: tuple[str, ...] = ()
    entity_universe: tuple[str, ...] = ()
    created_at_ns: int = 0
    engine_version: str = __version__
    schema_version: str = DATASET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        label(self.namespace)
        require_ns(self.created_at_ns)
        if not isinstance(self.interval, TimeRange) or not isinstance(self.settings, Settings):
            raise ValidationError("typed dataset range/settings required")
        if self.settings.sec.user_agent is not None:
            raise ValidationError("contact identity cannot enter a dataset definition")
        if (
            not isinstance(self.sources, tuple)
            or not self.sources
            or any(not isinstance(s, SourceSpec) for s in self.sources)
        ):
            raise ValidationError("immutable nonempty historical sources required")
        names = tuple(s.provider_id for s in self.sources)
        if names != tuple(sorted(set(names))):
            raise ValidationError("dataset providers must be sorted and unique")
        if not isinstance(self.partition_policy, PartitionPolicy) or not isinstance(
            self.archive_policy, RetentionPolicy
        ):
            raise ValidationError("typed dataset policies required")
        sorted_labels(self.event_families)
        if any(family not in {f.value for f in EventFamily} for family in self.event_families):
            raise ValidationError("unknown requested event family")
        sorted_labels(self.entity_universe)
        label(self.engine_version)
        if self.schema_version != DATASET_SCHEMA_VERSION:
            raise ValidationError("unsupported dataset code/schema version")

    def semantic_content(self) -> object:
        return (
            self.namespace,
            self.interval,
            self.sources,
            self.settings,
            self.partition_policy,
            self.archive_policy,
            self.event_families,
            self.entity_universe,
            self.engine_version,
            self.schema_version,
        )

    @property
    def dataset_id(self) -> str:
        return "dataset_" + fingerprint("eie:dataset-definition:v1", self.semantic_content())


@dataclass(frozen=True, slots=True)
class BackfillJob:
    definition: DatasetDefinition
    build_version: str = "1"
    parent_build_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.definition, DatasetDefinition):
            raise ValidationError("typed historical dataset definition required")
        label(self.build_version)
        if self.parent_build_id is not None:
            label(self.parent_build_id)

    @property
    def build_id(self) -> str:
        return "build_" + fingerprint(
            "eie:dataset-build:v1",
            (self.definition.dataset_id, self.build_version, self.parent_build_id),
        )

    @property
    def job_id(self) -> str:
        return "job_" + fingerprint("eie:backfill-job:v1", self.build_id)


def semantic_record(value: object) -> object:
    """Project local acquisition clocks out of historical content identity only."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "contract": f"{type(value).__module__}.{type(value).__name__}",
            **{
                f.name: semantic_record(getattr(value, f.name))
                for f in fields(value)
                if f.name
                not in {
                    "source_observed_at_ns",
                    "ingested_at_ns",
                    "available_from_ns",
                    "source_reference",
                    "last_updated_at_ns",
                }
            },
        }
    if isinstance(value, (list, tuple)):
        return tuple(semantic_record(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class HistoricalFact:
    provider_id: str
    resource_id: str
    logical_id: str
    source_revision: int
    source_available_ns: int | None
    quality: ReconstructionQuality
    raw: RawSourceObservation
    record: DomainRecord
    adapter_version: str
    parser_version: str
    provenance: ProvenanceKind = ProvenanceKind.HISTORICALLY_RECONSTRUCTED

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, ProvenanceKind):
            raise ValidationError("typed historical fact provenance required")
        identifier(self.provider_id)
        for value in (self.resource_id, self.logical_id, self.adapter_version, self.parser_version):
            label(value)
        if (
            isinstance(self.source_revision, bool)
            or not isinstance(self.source_revision, int)
            or self.source_revision < 1
        ):
            raise ValidationError("explicit positive source revision required")
        if (
            not isinstance(self.quality, ReconstructionQuality)
            or not isinstance(self.raw, RawSourceObservation)
            or not isinstance(self.record, RECORD_TYPES)
        ):
            raise ValidationError("typed historical fact/evidence required")
        if (
            self.raw.source.name != self.provider_id
            or self.raw.source.adapter_version != self.adapter_version
        ):
            raise ValidationError("historical adapter/source identity disagrees")
        evidence_record = getattr(self.record, "event", self.record)
        classes = getattr(
            evidence_record, "source_classes", frozenset({self.raw.source.source_class})
        )
        context = getattr(evidence_record, "source", None)
        if (
            classes != frozenset({self.raw.source.source_class})
            or getattr(context, "source_class", self.raw.source.source_class)
            is not self.raw.source.source_class
        ):
            raise ValidationError("historical record source claims disagree with raw evidence")
        primary = getattr(
            evidence_record,
            "source_observation_id",
            getattr(context, "source_observation_id", self.raw.observation_id),
        )
        evidence = getattr(evidence_record, "evidence_observation_ids", (self.raw.observation_id,))
        if primary != self.raw.observation_id or evidence != (self.raw.observation_id,):
            raise ValidationError(
                "historical record requires its linked single-fragment raw evidence"
            )
        if getattr(context, "provider_id", self.provider_id) != self.provider_id:
            raise ValidationError("historical record provider context disagrees with evidence")
        if self.source_available_ns is not None:
            require_ns(self.source_available_ns)
            if self.source_available_ns > self.raw.source_observed_at_ns:
                raise ValidationError("historical availability follows acquisition")
        exact = self.quality in (
            ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME,
            ReconstructionQuality.SOURCE_TIMESTAMP_RECONSTRUCTED,
        )
        if exact != (self.source_available_ns is not None):
            raise ValidationError("coarse/unknown availability cannot fabricate precision")
        if self.source_available_ns is not None:
            temporal_record = getattr(self.record, "event", self.record)
            for name in ("published_at_ns", "announced_at_ns", "accepted_at_ns"):
                source_time = getattr(temporal_record, name, None)
                if source_time is not None and source_time > self.source_available_ns:
                    raise ValidationError("historical availability predates source publication")
            context = getattr(self.record, "source", None)
            vintage = getattr(context, "vintage_at_ns", None)
            if vintage is not None and vintage > self.source_available_ns:
                raise ValidationError("historical availability predates estimate vintage")
        revision = getattr(
            self.record, "revision", getattr(getattr(self.record, "event", None), "revision", None)
        )
        number = getattr(
            revision, "number", getattr(self.record, "revision_number", self.source_revision)
        )
        if number != self.source_revision:
            raise ValidationError("historical source/domain revision disagree")

    @property
    def content_fingerprint(self) -> str:
        return fingerprint(
            "eie:historical-fact:v1",
            (
                self.provider_id,
                self.resource_id,
                self.logical_id,
                self.source_revision,
                self.source_available_ns,
                self.quality,
                self.raw.observation_id,
                semantic_record(self.record),
                self.adapter_version,
                self.parser_version,
            ),
        )

    @property
    def fact_id(self) -> str:
        return "fact_" + self.content_fingerprint


@dataclass(frozen=True, slots=True)
class HistoricalInput:
    """Audited interchange input; contains no claim of EIE local observation time."""

    source: SourceIdentity
    item_id: str
    item_version: str
    fragment_hash: str
    range_at_ns: int
    source_available_ns: int | None
    quality: ReconstructionQuality
    source_revision: int
    record: DomainRecord

    def __post_init__(self) -> None:
        require_ns(self.range_at_ns)
        if not isinstance(self.source, SourceIdentity) or not isinstance(self.record, RECORD_TYPES):
            raise ValidationError("typed audited historical input required")
        for value in (self.item_id, self.item_version):
            label(value)
        if not isinstance(self.fragment_hash, str) or SHA256.fullmatch(self.fragment_hash) is None:
            raise ValidationError("audited fragment SHA-256 required")
        if (
            not isinstance(self.quality, ReconstructionQuality)
            or isinstance(self.source_revision, bool)
            or not isinstance(self.source_revision, int)
            or self.source_revision < 1
        ):
            raise ValidationError("typed audited source revision/quality required")
        if self.source_available_ns is not None:
            require_ns(self.source_available_ns)


@dataclass(frozen=True, slots=True)
class LineageRecord:
    build_id: str
    partition_id: str
    acquisition_id: str
    fact_id: str
    observation_id: str
    object_hash: str
    adapter_version: str
    parser_version: str
    settings_fingerprint: str

    def __post_init__(self) -> None:
        for value in (
            self.build_id,
            self.partition_id,
            self.acquisition_id,
            self.fact_id,
            self.observation_id,
            self.object_hash,
            self.adapter_version,
            self.parser_version,
            self.settings_fingerprint,
        ):
            label(value)


@dataclass(frozen=True, slots=True)
class CompletenessReport:
    requested_partitions: int
    completed_partitions: int
    known_empty_partitions: int
    failed_partitions: int
    gapped_partitions: int
    pending_partitions: int
    split_partitions: int
    resources: tuple[tuple[str, str, int, int], ...]
    gap_reasons: tuple[tuple[str, int], ...] = ()
    reconstruction_quality: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        values = (
            self.completed_partitions,
            self.known_empty_partitions,
            self.failed_partitions,
            self.gapped_partitions,
            self.pending_partitions,
        )
        if (
            any(
                isinstance(v, bool) or not isinstance(v, int) or v < 0
                for v in (self.requested_partitions, self.split_partitions, *values)
            )
            or sum(values) != self.requested_partitions
        ):
            raise ValidationError("completeness partition counts disagree")
        for rows in (self.gap_reasons, self.reconstruction_quality):
            if rows != tuple(sorted(rows)) or any(count < 1 for _, count in rows):
                raise ValidationError("sorted positive completeness categories required")


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    job: BackfillJob
    status: JobStatus
    partitions: tuple[PartitionProgress, ...]
    gaps: tuple[GapRecord, ...]
    archive_hashes: tuple[str, ...]
    record_counts: tuple[tuple[str, int], ...]
    records_fingerprint: str
    partitions_fingerprint: str
    completeness: CompletenessReport
    completed_at_ns: int
    store_schema_version: str
    schema_version: str = DATASET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_ns(self.completed_at_ns)
        if self.status not in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_GAPS):
            raise ValidationError("manifest must describe a finalized build")
        if self.schema_version != DATASET_SCHEMA_VERSION or not isinstance(self.job, BackfillJob):
            raise ValidationError("unsupported manifest contract")
        if bool(self.gaps) != (self.status is JobStatus.COMPLETED_WITH_GAPS):
            raise ValidationError("manifest gap/status disagree")
        if tuple(p.partition_id for p in self.partitions) != tuple(
            sorted({p.partition_id for p in self.partitions})
        ):
            raise ValidationError("manifest partitions must be sorted and unique")
        sorted_labels(self.archive_hashes)
        if any(
            SHA256.fullmatch(h) is None
            for h in (*self.archive_hashes, self.records_fingerprint, self.partitions_fingerprint)
        ):
            raise ValidationError("manifest content SHA-256 required")
        if (
            not isinstance(self.completeness, CompletenessReport)
            or self.record_counts != tuple(sorted(self.record_counts))
            or len(dict(self.record_counts)) != len(self.record_counts)
            or any(
                isinstance(n, bool) or not isinstance(n, int) or n < 0
                for _, n in self.record_counts
            )
        ):
            raise ValidationError("manifest counts/completeness malformed")
        label(self.store_schema_version)

    @property
    def content_fingerprint(self) -> str:
        return fingerprint(
            "eie:dataset-content:v1",
            (
                self.job.definition.dataset_id,
                self.status,
                self.archive_hashes,
                self.record_counts,
                self.records_fingerprint,
                self.partitions_fingerprint,
                self.completeness,
                tuple(
                    sorted(
                        (g.semantic_content() for g in self.gaps),
                        key=lambda g: fingerprint("gap-order", g),
                    )
                ),
                self.store_schema_version,
                self.schema_version,
            ),
        )


class ValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_KNOWN_GAPS = "VALID_WITH_KNOWN_GAPS"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class DatasetValidationResult:
    build_id: str
    status: ValidationStatus
    errors: tuple[str, ...]
    content_fingerprint: str


class ReprocessPolicy(StrEnum):
    NO_REPROCESS = "NO_REPROCESS"
    REBUILD_SAME_VERSION = "REBUILD_SAME_VERSION"
    REPROCESS_WITH_NEW_VERSION = "REPROCESS_WITH_NEW_VERSION"
