"""Configured source capabilities and bounded atomic source-item contracts."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.events.models import CanonicalEvent
from eie.evidence.claims import Claim, ClaimValue, ordered
from eie.evidence.graph import ClaimRelationship
from eie.evidence.matching import EventDescriptor
from eie.evidence.references import AcquisitionLineage, EvidenceDataset, EvidenceReference
from eie.evidence.schema import ClaimStatus, ClaimType
from eie.ingestion.models import RetentionPermission, RetentionPolicy
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.studies.checks import contract, digest


@dataclass(frozen=True, slots=True)
class EvidenceProvider:
    source: SourceIdentity
    source_family: str
    dataset: EvidenceDataset
    capabilities: frozenset[ClaimType]
    parser_version: str = "structured-evidence-1"
    normalized_retention: RetentionPermission = RetentionPermission.UNKNOWN
    archive_policy: RetentionPolicy = RetentionPolicy()

    def __post_init__(self) -> None:
        contract(self)
        if not self.capabilities:
            raise ValidationError("explicit structured claim capabilities required")


@dataclass(frozen=True, slots=True)
class ClaimInput:
    provider_claim_key: str
    claim: Claim
    value: ClaimValue
    evidence_ids: tuple[str, ...]
    status: ClaimStatus = ClaimStatus.ASSERTED
    valid_from_ns: int | None = None
    valid_to_ns: int | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        self.claim.validate_value(self.value)
        ordered(self.evidence_ids, 4096, empty=False)


@dataclass(frozen=True, slots=True)
class EvidenceBatch:
    resource_id: str
    raw: RawSourceObservation
    lineage: AcquisitionLineage
    references: tuple[EvidenceReference, ...]
    claims: tuple[ClaimInput, ...]
    events: tuple[CanonicalEvent, ...] = ()
    descriptors: tuple[EventDescriptor, ...] = ()
    relationships: tuple[ClaimRelationship, ...] = ()
    format_version: str = "EIE_EVIDENCE_V1"

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.format_version != "EIE_EVIDENCE_V1"
            or len(self.claims) > 10000
            or len(self.references) > 10000
            or len(self.events) > 1000
            or len(self.descriptors) > 1000
            or len(self.relationships) > 10000
        ):
            raise ValidationError("supported bounded structured evidence interchange required")
        ordered(tuple(r.evidence_id for r in self.references), 10000)
        ordered(tuple(c.provider_claim_key for c in self.claims), 10000)
        ordered(tuple(e.revision_id for e in self.events), 1000)
        ordered(tuple(d.descriptor_id for d in self.descriptors), 1000)
        ordered(tuple(r.relationship_id for r in self.relationships), 10000)
        if self.raw.observation_id != self.lineage.raw_observation_id or (
            self.raw.source_observed_at_ns,
            self.raw.ingested_at_ns,
        ) != (self.lineage.observed_at_ns, self.lineage.ingested_at_ns):
            raise ValidationError("raw acquisition clocks and lineage disagree")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:structured-evidence-item:v1", self)


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    item_fingerprint: str
    dataset_id: str
    provider: str
    resource_id: str
    raw_observation_id: str
    lineage_id: str
    known_from_ns: int
    generation: int
    assertion_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    resolution_ids: tuple[str, ...]
    event_run_id: str | None
    policy_fingerprint: str

    def __post_init__(self) -> None:
        contract(self)
        digest(self.item_fingerprint)
        digest(self.policy_fingerprint)
        if self.generation < 1:
            raise ValidationError("positive evidence checkpoint generation required")
        for values in (
            self.assertion_ids,
            self.evidence_ids,
            self.relationship_ids,
            self.resolution_ids,
        ):
            ordered(values, 100000)

    @property
    def receipt_id(self) -> str:
        return "ereceipt_" + fingerprint(
            "eie:evidence-item-receipt:v1",
            (self.item_fingerprint, self.dataset_id, self.policy_fingerprint),
        )

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:evidence-receipt:v1", self)
