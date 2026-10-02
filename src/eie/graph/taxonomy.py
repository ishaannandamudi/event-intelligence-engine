"""Controlled node/relationship types and explicit inverse/symmetric semantics."""

from enum import StrEnum


class GraphNodeType(StrEnum):
    ISSUER = "ISSUER"
    LEGAL_ENTITY = "LEGAL_ENTITY"
    SECURITY = "SECURITY"
    LISTING = "LISTING"
    ETF = "ETF"
    INDEX = "INDEX"
    SECTOR = "SECTOR"
    INDUSTRY = "INDUSTRY"
    FUND = "FUND"
    GOVERNMENT_AGENCY = "GOVERNMENT_AGENCY"


class RelationshipType(StrEnum):
    PARENT_OF = "PARENT_OF"
    SUBSIDIARY_OF = "SUBSIDIARY_OF"
    SUPPLIER_TO = "SUPPLIER_TO"
    CUSTOMER_OF = "CUSTOMER_OF"
    COMPETITOR_OF = "COMPETITOR_OF"
    PEER_OF = "PEER_OF"
    SECTOR_MEMBER_OF = "SECTOR_MEMBER_OF"
    INDUSTRY_MEMBER_OF = "INDUSTRY_MEMBER_OF"
    INDEX_MEMBER_OF = "INDEX_MEMBER_OF"
    ETF_HOLDS = "ETF_HOLDS"
    OWNS_EQUITY_IN = "OWNS_EQUITY_IN"
    OWNED_BY = "OWNED_BY"
    SPINOFF_FROM = "SPINOFF_FROM"
    SUCCESSOR_OF = "SUCCESSOR_OF"
    PREDECESSOR_OF = "PREDECESSOR_OF"
    ISSUER_ISSUES_SECURITY = "ISSUER_ISSUES_SECURITY"
    SECURITY_HAS_LISTING = "SECURITY_HAS_LISTING"
    LISTING_REPRESENTS_SECURITY = "LISTING_REPRESENTS_SECURITY"


class Direction(StrEnum):
    OUTGOING = "OUTGOING"
    INCOMING = "INCOMING"
    BOTH = "BOTH"


SYMMETRIC = frozenset({RelationshipType.COMPETITOR_OF, RelationshipType.PEER_OF})
INVERSES = {
    RelationshipType.SUBSIDIARY_OF: RelationshipType.PARENT_OF,
    RelationshipType.CUSTOMER_OF: RelationshipType.SUPPLIER_TO,
    RelationshipType.OWNED_BY: RelationshipType.OWNS_EQUITY_IN,
    RelationshipType.PREDECESSOR_OF: RelationshipType.SUCCESSOR_OF,
    RelationshipType.LISTING_REPRESENTS_SECURITY: RelationshipType.SECURITY_HAS_LISTING,
}
MEMBERSHIP = frozenset({RelationshipType.INDEX_MEMBER_OF, RelationshipType.ETF_HOLDS})


def canonical_type(kind: RelationshipType) -> RelationshipType:
    return INVERSES.get(kind, kind)


def canonical_endpoints(
    kind: RelationshipType, source: str, target: str
) -> tuple[RelationshipType, str, str]:
    if kind in INVERSES:
        source, target = target, source
    if kind in SYMMETRIC and source > target:
        source, target = target, source
    return canonical_type(kind), source, target
