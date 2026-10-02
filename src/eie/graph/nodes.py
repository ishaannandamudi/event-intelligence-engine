"""Reuse stable issuer/security/listing identities; never create ticker nodes."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.entities.models import Issuer, Security
from eie.entities.sec import SecIssuerRevision, SecListingRevision
from eie.graph.taxonomy import GraphNodeType
from eie.sources.models import SourceIdentity
from eie.studies.checks import contract, digest


@dataclass(frozen=True, slots=True)
class GraphDataset:
    dataset_id: str
    fingerprint: str
    version: str
    build_id: str | None = None

    def __post_init__(self) -> None:
        contract(self)
        digest(self.fingerprint)


@dataclass(frozen=True, slots=True)
class NodeIdentifier:
    authority: str
    value: str

    def __post_init__(self) -> None:
        contract(self)
        if self.authority.upper() in ("TICKER", "SYMBOL", "NAME"):
            raise ValidationError("stable non-ticker node identifier required")


def node_id(kind: GraphNodeType, authority: str, stable_identifier: str) -> str:
    NodeIdentifier(authority, stable_identifier)
    if not isinstance(kind, GraphNodeType) or kind in (
        GraphNodeType.ISSUER,
        GraphNodeType.SECURITY,
        GraphNodeType.LISTING,
    ):
        raise ValidationError("issuer/security/listing nodes must reuse foundation identities")
    return "nod_" + fingerprint("eie:graph-node:v1", (kind, authority, stable_identifier))


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    node_type: GraphNodeType
    label: str
    known_from_ns: int
    source: SourceIdentity
    dataset: GraphDataset
    identifiers: tuple[NodeIdentifier, ...] = ()
    foundation: Issuer | SecIssuerRevision | Security | SecListingRevision | None = None
    identity_identifier: NodeIdentifier | None = None

    def __post_init__(self) -> None:
        contract(self)
        keys = tuple((v.authority, v.value) for v in self.identifiers)
        if len(keys) > 32 or keys != tuple(sorted(set(keys))):
            raise ValidationError("bounded sorted distinct node identifiers required")
        f = self.foundation
        if self.node_type is GraphNodeType.ISSUER:
            if not isinstance(f, (Issuer, SecIssuerRevision)) or self.node_id != f.issuer_id:
                raise ValidationError("reuse verified issuer contract")
            if isinstance(f, SecIssuerRevision) and self.known_from_ns < f.available_from_ns:
                raise ValidationError("issuer cannot precede foundation knowledge")
            cik = f.cik.digits if isinstance(f, SecIssuerRevision) else f.cik
            if cik is not None and NodeIdentifier("SEC_CIK", cik) not in self.identifiers:
                raise ValidationError("preserve exact foundation CIK")
            if any(i.authority == "SEC_CIK" and i.value != cik for i in self.identifiers):
                raise ValidationError("CIK mappings must agree with verified issuer foundation")
        elif self.node_type in (GraphNodeType.SECURITY, GraphNodeType.ETF) and isinstance(
            f, Security
        ):
            if self.node_id != f.security_id:
                raise ValidationError("reuse verified security identity")
        elif self.node_type is GraphNodeType.LISTING:
            if not isinstance(f, SecListingRevision) or self.node_id != f.listing_id:
                raise ValidationError("reuse verified listing contract")
            if self.known_from_ns < f.available_from_ns:
                raise ValidationError("listing cannot precede foundation knowledge")
        elif self.node_type is GraphNodeType.SECURITY or f is not None:
            raise ValidationError("node/foundation type mismatch")
        else:
            primary = self.identity_identifier or (
                self.identifiers[0] if self.identifiers else None
            )
            if (
                primary is None
                or primary not in self.identifiers
                or self.node_id != node_id(self.node_type, primary.authority, primary.value)
            ):
                raise ValidationError("graph-specific node requires sourced stable identity")

    @property
    def revision_id(self) -> str:
        return "node_rev_" + fingerprint("eie:graph-node-revision:v1", self)


def foundation_node(
    value: Issuer | SecIssuerRevision | Security | SecListingRevision,
    source: SourceIdentity,
    dataset: GraphDataset,
    known_from_ns: int,
) -> GraphNode:
    identifiers: tuple[NodeIdentifier, ...] = ()
    if isinstance(value, (Issuer, SecIssuerRevision)):
        identity, kind, label = value.issuer_id, GraphNodeType.ISSUER, value.legal_name
        cik = value.cik.digits if isinstance(value, SecIssuerRevision) else value.cik
        if cik is not None:
            identifiers = (NodeIdentifier("SEC_CIK", cik),)
    elif isinstance(value, Security):
        identity, kind, label = value.security_id, GraphNodeType.SECURITY, value.description
    else:
        identity, kind, label = value.listing_id, GraphNodeType.LISTING, value.ticker
    return GraphNode(identity, kind, label, known_from_ns, source, dataset, identifiers, value)
