"""Immutable provider assertions with independent valid and knowledge clocks."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.graph import GRAPH_SCHEMA_VERSION
from eie.graph.exposure import ExposureMeasurement, HoldingQuantity
from eie.graph.nodes import GraphDataset
from eie.graph.taxonomy import (
    INVERSES,
    GraphNodeType,
    RelationshipType,
    canonical_endpoints,
    canonical_type,
)
from eie.sources.models import SourceIdentity
from eie.studies.checks import contract


class IdentifierKind(StrEnum):
    NODE_ID = "NODE_ID"
    CIK = "CIK"
    PROVIDER_ID = "PROVIDER_ID"
    SYMBOL = "SYMBOL"


class ValidSemantics(StrEnum):
    INTERVAL = "INTERVAL"
    SNAPSHOT = "SNAPSHOT"
    UNKNOWN = "UNKNOWN"


class ClaimState(StrEnum):
    ASSERTED = "ASSERTED"
    RETRACTED = "RETRACTED"


class ClaimRevisionKind(StrEnum):
    INITIAL = "INITIAL"
    UPDATE = "UPDATE"
    CORRECTION = "CORRECTION"
    TERMINATION = "TERMINATION"


class GraphQuality(StrEnum):
    UNRESOLVED_ENTITY = "UNRESOLVED_ENTITY"
    CONFLICTING_SOURCE = "CONFLICTING_SOURCE"
    STALE_EXPOSURE = "STALE_EXPOSURE"
    UNKNOWN_VALID_FROM = "UNKNOWN_VALID_FROM"
    UNKNOWN_VALID_TO = "UNKNOWN_VALID_TO"
    PARTIAL_SNAPSHOT = "PARTIAL_SNAPSHOT"
    CARRIED_SNAPSHOT = "CARRIED_SNAPSHOT"
    IMPLIED_TERMINATION = "IMPLIED_TERMINATION"
    COARSE_DATE = "COARSE_DATE"
    ESTIMATED_VALUE = "ESTIMATED_VALUE"
    SOURCE_RECONSTRUCTION_LIMITATION = "SOURCE_RECONSTRUCTION_LIMITATION"
    EXPOSURE_UNAVAILABLE = "EXPOSURE_UNAVAILABLE"
    TRUNCATED = "TRUNCATED"


@dataclass(frozen=True, slots=True)
class Endpoint:
    kind: IdentifierKind
    value: str
    expected_type: GraphNodeType
    authority: str | None = None
    exchange: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        if self.kind is IdentifierKind.SYMBOL:
            if self.exchange is None or self.expected_type is not GraphNodeType.ISSUER:
                raise ValidationError("symbol resolution requires exact exchange and issuer type")
        elif self.exchange is not None:
            raise ValidationError("exchange is only for historical symbol resolution")
        if (self.kind is IdentifierKind.PROVIDER_ID) != (self.authority is not None):
            raise ValidationError("provider identifiers require a stable authority")
        if self.kind is IdentifierKind.CIK and (
            len(self.value) != 10
            or not self.value.isascii()
            or not self.value.isdecimal()
            or self.expected_type is not GraphNodeType.ISSUER
        ):
            raise ValidationError("exact normalized issuer CIK required")


@dataclass(frozen=True, slots=True)
class RelationshipItem:
    claim_key: str
    relationship_type: RelationshipType
    source_endpoint: Endpoint
    target_endpoint: Endpoint
    valid_from_ns: int | None
    valid_to_ns: int | None = None
    valid_semantics: ValidSemantics = ValidSemantics.INTERVAL
    state: ClaimState = ClaimState.ASSERTED
    revision_kind: ClaimRevisionKind = ClaimRevisionKind.INITIAL
    exposure: ExposureMeasurement | None = None
    holding: HoldingQuantity | None = None
    qualifiers: tuple[tuple[str, str], ...] = ()
    quality: frozenset[GraphQuality] = frozenset()
    direct_disclosure: bool = True

    def __post_init__(self) -> None:
        contract(self)
        if self.valid_semantics is ValidSemantics.UNKNOWN:
            if self.valid_from_ns is not None or self.valid_to_ns is not None:
                raise ValidationError("unknown validity cannot fabricate dates")
        elif self.valid_from_ns is None:
            raise ValidationError("declared interval/snapshot needs a valid time")
        if self.valid_to_ns is not None and (
            self.valid_from_ns is None
            or self.valid_to_ns <= self.valid_from_ns
            or self.valid_semantics is not ValidSemantics.INTERVAL
        ):
            raise ValidationError("positive half-open valid interval required")
        if self.revision_kind is ClaimRevisionKind.TERMINATION and self.valid_to_ns is None:
            raise ValidationError("termination requires an explicit valid end")
        keys = tuple(k for k, _ in self.qualifiers)
        if (
            len(keys) > 16
            or keys != tuple(sorted(set(keys)))
            or any(
                not k
                or not v
                or k != k.strip()
                or v != v.strip()
                or len(k) > 256
                or len(v) > 256
                or any(ord(c) < 32 for c in k + v)
                for k, v in self.qualifiers
            )
        ):
            raise ValidationError("bounded sorted sourced qualifiers required")
        if self.holding is not None and self.relationship_type is not RelationshipType.ETF_HOLDS:
            raise ValidationError("holding quantities require ETF holding semantics")


def edge_identity(
    kind: RelationshipType, source: str, target: str, qualifiers: tuple[tuple[str, str], ...]
) -> str:
    return "edge_" + fingerprint(
        "eie:relationship-edge:v1", (*canonical_endpoints(kind, source, target), qualifiers)
    )


@dataclass(frozen=True, slots=True)
class ProviderRelationshipClaim:
    source: SourceIdentity
    dataset: GraphDataset
    resource_id: str
    claim_key: str
    item: RelationshipItem
    observation_id: str
    number: int
    known_from_ns: int
    source_node_id: str | None
    target_node_id: str | None
    previous_revision_id: str | None = None
    quality: frozenset[GraphQuality] = frozenset()
    schema_version: str = GRAPH_SCHEMA_VERSION
    resolution_valid_at_ns: int | None = None

    def __post_init__(self) -> None:
        contract(self)
        if self.schema_version != GRAPH_SCHEMA_VERSION or self.number < 1:
            raise ValidationError("supported graph schema and positive claim revision required")
        if not self.observation_id.startswith("obs_"):
            raise ValidationError("relationship claim requires raw observation evidence")
        if (self.number == 1) != (self.previous_revision_id is None):
            raise ValidationError("claim revision predecessor required")
        if self.source_node_id is not None and self.source_node_id == self.target_node_id:
            raise ValidationError("canonical self-relationship refused")
        for measurement in (self.item.exposure, self.item.holding):
            if measurement is not None and measurement.reference_ns > self.known_from_ns:
                raise ValidationError("measurement reference cannot follow knowledge")

    @property
    def claim_id(self) -> str:
        return "claim_" + fingerprint(
            "eie:relationship-claim:v1", (self.source.name, self.resource_id, self.claim_key)
        )

    @property
    def revision_id(self) -> str:
        return "claim_rev_" + fingerprint("eie:relationship-claim-revision:v1", self)

    @property
    def edge_id(self) -> str | None:
        if self.source_node_id is None or self.target_node_id is None:
            return None
        return edge_identity(
            self.item.relationship_type,
            self.source_node_id,
            self.target_node_id,
            self.item.qualifiers,
        )

    @property
    def endpoints(self) -> tuple[RelationshipType, str | None, str | None]:
        if self.source_node_id is None or self.target_node_id is None:
            kind = self.item.relationship_type
            source, target = self.source_node_id, self.target_node_id
            if kind in INVERSES:
                source, target = target, source
            return canonical_type(kind), source, target
        return canonical_endpoints(
            self.item.relationship_type, self.source_node_id, self.target_node_id
        )


def claim_active(claim: ProviderRelationshipClaim, valid_at_ns: int) -> bool | None:
    item = claim.item
    if item.valid_from_ns is None or valid_at_ns < item.valid_from_ns:
        return None
    if item.valid_semantics is ValidSemantics.SNAPSHOT and valid_at_ns != item.valid_from_ns:
        return None
    return item.state is ClaimState.ASSERTED and (
        item.valid_to_ns is None or valid_at_ns < item.valid_to_ns
    )
