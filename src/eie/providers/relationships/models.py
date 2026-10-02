"""Bounded audited source items; snapshot completeness and carry are declarations."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.graph.claims import Endpoint, RelationshipItem
from eie.graph.models import DEFAULT_GRAPH_POLICY, GraphPolicy
from eie.graph.nodes import GraphDataset
from eie.graph.taxonomy import MEMBERSHIP, RelationshipType
from eie.sources.models import SourceIdentity
from eie.studies.checks import contract


class SnapshotPolicy(StrEnum):
    POINT_ONLY = "POINT_ONLY"
    CARRY_UNTIL_NEXT = "CARRY_UNTIL_NEXT"


@dataclass(frozen=True, slots=True)
class RelationshipProvider:
    source: SourceIdentity
    dataset: GraphDataset
    capabilities: frozenset[RelationshipType]
    parser_version: str = "relationship-interchange-1"
    snapshot_policy: SnapshotPolicy = SnapshotPolicy.POINT_ONLY
    allow_implied_removal: bool = False
    long_only_portfolio: bool = True

    def __post_init__(self) -> None:
        contract(self)
        if not self.capabilities:
            raise ValidationError("explicit relationship provider capabilities required")


@dataclass(frozen=True, slots=True)
class SnapshotScope:
    owner: Endpoint
    relationship_type: RelationshipType
    effective_ns: int
    exhaustive: bool = False
    remove_missing: bool = False

    def __post_init__(self) -> None:
        contract(self)
        if self.relationship_type not in MEMBERSHIP:
            raise ValidationError("composition snapshots are explicit index or ETF membership")
        if self.remove_missing and not self.exhaustive:
            raise ValidationError("partial snapshot cannot imply removal")


@dataclass(frozen=True, slots=True)
class RelationshipBatch:
    resource_id: str
    source_item_id: str
    source_item_version: str
    source_observed_at_ns: int
    ingested_at_ns: int
    items: tuple[RelationshipItem, ...]
    published_at_ns: int | None = None
    snapshot: SnapshotScope | None = None
    source_reference: str | None = None
    format_version: str = "EIE_RELATIONSHIPS_V1"

    def __post_init__(self) -> None:
        contract(self)
        if self.format_version != "EIE_RELATIONSHIPS_V1" or len(self.items) > 10000:
            raise ValidationError("supported bounded relationship interchange required")
        if tuple(i.claim_key for i in self.items) != tuple(
            sorted({i.claim_key for i in self.items})
        ):
            raise ValidationError("ordered distinct source claim keys required")
        if (
            self.source_observed_at_ns > self.ingested_at_ns
            or self.published_at_ns is not None
            and self.published_at_ns > self.source_observed_at_ns
        ):
            raise ValidationError(
                "relationship publication/observation/ingestion clocks out of order"
            )

    @property
    def snapshot_id(self) -> str:
        return "provider_snapshot_" + fingerprint("eie:relationship-source-item:v1", self)


@dataclass(frozen=True, slots=True)
class GraphIngestionResult:
    batch_id: str
    observation_id: str
    provider: RelationshipProvider
    batch: RelationshipBatch
    claim_revision_ids: tuple[str, ...]
    canonical_revision_ids: tuple[str, ...]
    checkpoint_generation: int
    unresolved_count: int
    conflict_count: int
    policy: GraphPolicy = DEFAULT_GRAPH_POLICY

    def __post_init__(self) -> None:
        contract(self)
        if self.checkpoint_generation < 1 or self.unresolved_count < 0 or self.conflict_count < 0:
            raise ValidationError("valid ingestion counts required")
        if self.batch_id != "graph_batch_" + fingerprint(
            "eie:graph-ingestion-identity:v1", (self.provider, self.batch, self.policy)
        ):
            raise ValidationError(
                "ingestion identity must pin provider, parser, dataset and source item"
            )
        if (
            len(self.claim_revision_ids) < len(self.batch.items)
            or tuple(sorted(set(self.claim_revision_ids))) != self.claim_revision_ids
            or tuple(sorted(set(self.canonical_revision_ids))) != self.canonical_revision_ids
        ):
            raise ValidationError(
                "ordered distinct complete source-item revision references required"
            )

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:graph-ingestion:v1", self)
