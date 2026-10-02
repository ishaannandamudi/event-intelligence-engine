"""Resolved endpoints and explicit structured semantics only; no fuzzy text extraction."""

from eie.core.errors import ValidationError
from eie.graph.claims import IdentifierKind, ProviderRelationshipClaim
from eie.graph.exposure import ExposureType
from eie.graph.history import GraphReader
from eie.graph.nodes import GraphNode
from eie.graph.taxonomy import GraphNodeType as N
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.taxonomy import canonical_type


def validate_endpoints(claim: ProviderRelationshipClaim, reader: GraphReader) -> None:
    from eie.entities.sec import SecListingRevision

    for endpoint, identity in (
        (claim.item.source_endpoint, claim.source_node_id),
        (claim.item.target_endpoint, claim.target_node_id),
    ):
        if identity is None:
            continue
        node = reader.node(identity, claim.known_from_ns)
        if node is None or node.node_type is not endpoint.expected_type:
            raise ValidationError("resolved endpoint type/knowledge disagrees with source item")
        if endpoint.kind is IdentifierKind.NODE_ID and endpoint.value != identity:
            raise ValidationError("resolved endpoint disagrees with explicit stable source ID")
        if endpoint.kind in (IdentifierKind.CIK, IdentifierKind.PROVIDER_ID):
            authority = "SEC_CIK" if endpoint.kind is IdentifierKind.CIK else endpoint.authority
            if not any(
                i.authority == authority and i.value == endpoint.value for i in node.identifiers
            ):
                raise ValidationError("resolved endpoint disagrees with audited exact identifier")
        if endpoint.kind is IdentifierKind.SYMBOL:
            at = claim.resolution_valid_at_ns
            if at is None or endpoint.exchange is None:
                raise ValidationError("symbol claim requires pinned valid-time resolution")
            listings = reader.symbol_listings(
                endpoint.value, endpoint.exchange, claim.known_from_ns, at
            )
            if {
                n.foundation.issuer_id
                for n in listings
                if isinstance(n.foundation, SecListingRevision)
            } != {identity}:
                raise ValidationError("resolved endpoint disagrees with historical symbol evidence")
    kind, source_id, target_id = claim.endpoints
    if source_id is None or target_id is None:
        return
    source, target = (
        reader.node(source_id, claim.known_from_ns),
        reader.node(target_id, claim.known_from_ns),
    )
    if source is None or target is None:
        raise ValidationError("resolved relationship endpoints must exist at knowledge cutoff")
    organizations = (N.ISSUER, N.LEGAL_ENTITY, N.FUND, N.GOVERNMENT_AGENCY, N.ETF)
    if kind is R.PARENT_OF and (
        source.node_type not in organizations or target.node_type not in organizations
    ):
        raise ValidationError("parent hierarchy requires organizational endpoints")
    expected: dict[R, tuple[tuple[N, ...], tuple[N, ...]]] = {
        R.SUPPLIER_TO: (organizations, organizations),
        R.COMPETITOR_OF: (organizations, organizations),
        R.PEER_OF: (organizations, organizations),
        R.SECTOR_MEMBER_OF: (organizations + (N.SECURITY,), (N.SECTOR,)),
        R.INDUSTRY_MEMBER_OF: (organizations + (N.SECURITY,), (N.INDUSTRY,)),
        R.INDEX_MEMBER_OF: ((N.ISSUER, N.SECURITY, N.ETF), (N.INDEX,)),
        R.ETF_HOLDS: ((N.ETF,), (N.SECURITY, N.ETF)),
        R.OWNS_EQUITY_IN: (organizations, organizations + (N.SECURITY,)),
        R.SPINOFF_FROM: (organizations, organizations),
        R.SUCCESSOR_OF: (organizations, organizations),
        R.ISSUER_ISSUES_SECURITY: ((N.ISSUER,), (N.SECURITY, N.ETF)),
        R.SECURITY_HAS_LISTING: ((N.SECURITY, N.ETF), (N.LISTING,)),
    }
    if kind in expected and (
        source.node_type not in expected[kind][0] or target.node_type not in expected[kind][1]
    ):
        raise ValidationError("relationship taxonomy and node types disagree")
    verify_foundation(kind, source, target)


def verify_foundation(kind: R, source: GraphNode, target: GraphNode) -> None:
    from eie.entities.models import Security
    from eie.entities.sec import SecListingRevision

    if kind is R.ISSUER_ISSUES_SECURITY and (
        not isinstance(target.foundation, Security) or target.foundation.issuer_id != source.node_id
    ):
        raise ValidationError("issuer-security relationship disagrees with verified foundation")
    if kind is R.SECURITY_HAS_LISTING and (
        not isinstance(target.foundation, SecListingRevision)
        or target.foundation.security_id != source.node_id
    ):
        raise ValidationError("security-listing relationship disagrees with verified foundation")


def validate_exposure(claim: ProviderRelationshipClaim) -> None:
    value = claim.item.exposure
    if value is None:
        return
    compatible = {
        R.PARENT_OF: (
            ExposureType.OWNERSHIP_PERCENT,
            ExposureType.VOTING_POWER,
            ExposureType.ECONOMIC_INTEREST,
        ),
        R.OWNS_EQUITY_IN: (
            ExposureType.OWNERSHIP_PERCENT,
            ExposureType.VOTING_POWER,
            ExposureType.ECONOMIC_INTEREST,
        ),
        R.SUPPLIER_TO: (ExposureType.REVENUE_SHARE, ExposureType.PURCHASE_SHARE),
        R.ETF_HOLDS: (ExposureType.PORTFOLIO_WEIGHT,),
        R.INDEX_MEMBER_OF: (ExposureType.INDEX_WEIGHT,),
    }
    if value.measurement_type not in compatible.get(
        canonical_type(claim.item.relationship_type), ()
    ):
        raise ValidationError("exposure type incompatible with relationship")
