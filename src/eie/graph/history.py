"""Bitemporal selection and deterministic source resolution with all alternatives."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.graph.claims import GraphQuality, ProviderRelationshipClaim, claim_active
from eie.graph.exposure import ExposureType
from eie.graph.models import GraphPolicy, RelationshipEdge
from eie.graph.nodes import GraphNode
from eie.graph.taxonomy import RelationshipType
from eie.studies.checks import contract


class GraphLimitError(ValidationError):
    """A declared input bound prevents a complete historical view."""


class GraphReader(Protocol):
    def node(self, node_id: str, known_as_of_ns: int) -> GraphNode | None: ...

    def symbol_listings(
        self, ticker: str, exchange: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[GraphNode, ...]: ...

    def current_claims(
        self,
        known_as_of_ns: int,
        *,
        node_id: str | None = None,
        edge_id: str | None = None,
        relationship_types: tuple[RelationshipType, ...] = (),
        provider: str | None = None,
        limit: int = 10000,
    ) -> tuple[ProviderRelationshipClaim, ...]: ...

    def search_history(self, query: GraphHistoryQuery) -> tuple[ClaimKnowledgeInterval, ...]: ...


def meaning(claim: ProviderRelationshipClaim, valid_at_ns: int) -> tuple[object, ...]:
    value = claim.item.exposure
    measurement = (
        None
        if value is None
        else (
            value.measurement_type,
            value.point,
            value.lower,
            value.upper,
            value.lower_inclusive,
            value.upper_inclusive,
            value.domain,
        )
    )
    holding = claim.item.holding
    quantity = None if holding is None else (holding.shares, holding.market_value, holding.currency)
    return claim_active(claim, valid_at_ns), measurement, quantity


def resolve_edges(
    claims: tuple[ProviderRelationshipClaim, ...],
    known_as_of_ns: int,
    valid_at_ns: int,
    policy: GraphPolicy,
) -> tuple[RelationshipEdge, ...]:
    groups: dict[str, list[ProviderRelationshipClaim]] = defaultdict(list)
    latest: dict[str, ProviderRelationshipClaim] = {}
    for claim in claims:
        if claim.known_from_ns <= known_as_of_ns and (
            claim.claim_id not in latest or claim.number > latest[claim.claim_id].number
        ):
            latest[claim.claim_id] = claim
    for claim in latest.values():
        if claim.edge_id is not None and claim_active(claim, valid_at_ns) is not None:
            groups[claim.edge_id].append(claim)
    edges = []
    for identity in sorted(groups):
        # New effective episodes supersede older episodes only within that provider/resource.
        episodes: dict[tuple[str, str], int] = {}
        for c in groups[identity]:
            key = c.source.name, c.resource_id
            episodes[key] = max(episodes.get(key, 0), c.item.valid_from_ns or 0)
        rows = tuple(
            sorted(
                (
                    c
                    for c in groups[identity]
                    if c.item.valid_from_ns == episodes[(c.source.name, c.resource_id)]
                ),
                key=lambda c: c.claim_id,
            )
        )
        order = policy.precedence.order_for("graph." + rows[0].endpoints[0].value.lower())
        preferred = min(
            rows,
            key=lambda c: (
                order.index(c.source.source_class),
                c.source.name,
                c.resource_id,
                c.claim_id,
            ),
        )
        reasons = set()
        signatures = {meaning(c, valid_at_ns) for c in rows}
        if len(signatures) > 1:
            reasons.add("SOURCE_STATE_OR_EXPOSURE_DISAGREEMENT")
        if (
            len({(c.item.valid_from_ns, c.item.valid_to_ns, c.item.valid_semantics) for c in rows})
            > 1
        ):
            reasons.add("SOURCE_VALIDITY_DISAGREEMENT")
        quality = {q for c in rows for q in c.quality | c.item.quality}
        if reasons:
            quality.add(GraphQuality.CONFLICTING_SOURCE)
            best = order.index(preferred.source.source_class)
            if (
                len(
                    {
                        meaning(c, valid_at_ns)
                        for c in rows
                        if order.index(c.source.source_class) == best
                    }
                )
                > 1
            ):
                reasons.add("EQUAL_PRECEDENCE_DISAGREEMENT")
        value = preferred.item.exposure
        if value is not None:
            if value.estimated:
                quality.add(GraphQuality.ESTIMATED_VALUE)
            if (
                policy.stale_exposure_ns is not None
                and known_as_of_ns - value.reference_ns > policy.stale_exposure_ns
            ):
                quality.add(GraphQuality.STALE_EXPOSURE)
        if preferred.item.valid_to_ns is None:
            quality.add(GraphQuality.UNKNOWN_VALID_TO)
        kind, source, target = preferred.endpoints
        assert source is not None and target is not None
        confirmations = len(
            {
                c.source.name
                for c in rows
                if meaning(c, valid_at_ns) == meaning(preferred, valid_at_ns)
            }
        )
        edges.append(
            RelationshipEdge(
                kind,
                source,
                target,
                preferred.item.qualifiers,
                bool(claim_active(preferred, valid_at_ns)),
                preferred,
                rows,
                confirmations,
                tuple(sorted(reasons)),
                frozenset(quality),
                policy,
            )
        )
    return tuple(edges)


@dataclass(frozen=True, slots=True)
class GraphHistoryQuery:
    """Claim revision search; optional intervals overlap half-open historical periods."""

    known_as_of_ns: int
    node_id: str | None = None
    relationship_types: tuple[RelationshipType, ...] = ()
    provider: str | None = None
    valid_interval: tuple[int, int] | None = None
    known_interval: tuple[int, int] | None = None
    exposure_type: ExposureType | None = None
    limit: int = 10000

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.known_as_of_ns)
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValidationError("history relationship filters must be distinct")
        for interval in (self.valid_interval, self.known_interval):
            if interval is not None:
                for boundary in interval:
                    require_ns(boundary)
                if interval[0] >= interval[1]:
                    raise ValidationError("history intervals must be nonempty and half-open")
        if not 1 <= self.limit <= 100000:
            raise ValidationError("bounded history query required")


@dataclass(frozen=True, slots=True)
class ClaimKnowledgeInterval:
    claim: ProviderRelationshipClaim
    known_to_ns: int | None

    def __post_init__(self) -> None:
        contract(self)
        if self.known_to_ns is not None and self.known_to_ns <= self.claim.known_from_ns:
            raise ValidationError("knowledge interval must advance past its revision")


def knowledge_intervals(
    revisions: tuple[ProviderRelationshipClaim, ...], known_as_of_ns: int
) -> tuple[ClaimKnowledgeInterval, ...]:
    rows = tuple(c for c in revisions if c.known_from_ns <= known_as_of_ns)
    return tuple(
        ClaimKnowledgeInterval(c, rows[i + 1].known_from_ns if i + 1 < len(rows) else None)
        for i, c in enumerate(rows)
    )
