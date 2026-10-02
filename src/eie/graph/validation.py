"""Pure artifact checks and optional complete indexed historical recalculation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from eie.core.errors import EIEError, ValidationError
from eie.entities.sec import SecListingRevision
from eie.graph.claims import (
    ClaimRevisionKind,
    GraphQuality,
    ProviderRelationshipClaim,
    claim_active,
)
from eie.graph.history import GraphReader, resolve_edges
from eie.graph.models import GraphPolicy, GraphSnapshot
from eie.graph.propagation import PropagationResult, candidates_for, event_roots
from eie.graph.taxonomy import SYMMETRIC, Direction, RelationshipType, canonical_type
from eie.graph.traversal import make_path, traverse
from eie.providers.relationships.models import GraphIngestionResult
from eie.studies.checks import contract

if TYPE_CHECKING:
    from eie.graph.repository import GraphRepository


def validate_claim_history(
    revisions: tuple[ProviderRelationshipClaim, ...], reader: GraphReader | None = None
) -> GraphValidationResult:
    from eie.providers.relationships.normalization import validate_endpoints, validate_exposure

    reasons = set()
    for i, value in enumerate(revisions):
        if (
            value.number != i + 1
            or value.previous_revision_id != (None if i == 0 else revisions[i - 1].revision_id)
            or i > 0
            and (
                value.claim_id != revisions[0].claim_id
                or value.known_from_ns <= revisions[i - 1].known_from_ns
            )
        ):
            reasons.add("INVALID_CLAIM_REVISION_CHAIN")
        try:
            validate_exposure(value)
            if reader is not None:
                validate_endpoints(value, reader)
        except EIEError:
            reasons.add("INVALID_CLAIM_ENDPOINT_OR_EXPOSURE")
    return GraphValidationResult(not reasons, tuple(sorted(reasons)))


def validate_ingestion(
    value: GraphIngestionResult, repository: GraphRepository
) -> GraphValidationResult:
    from eie.providers.relationships.adapter import RelationshipAdapter

    reasons = set()
    raw = repository.store.get_observation(value.observation_id)
    batch, p = value.batch, value.provider
    if (
        raw is None
        or raw.source != p.source
        or raw.source_item_id != batch.source_item_id
        or raw.source_item_version != batch.source_item_version
        or raw.ingested_at_ns != batch.ingested_at_ns
        or dict(raw.metadata).get("parser_version") != p.parser_version
    ):
        reasons.add("INVALID_SOURCE_ITEM_PROVENANCE")
    try:
        from eie.persistence import codec

        RelationshipAdapter(p).parse(codec.dumps(batch))
        values = tuple(repository.claim_revision(identity) for identity in value.claim_revision_ids)
        if any(c is None for c in values):
            reasons.add("MISSING_INGESTED_CLAIM")
        claims = tuple(c for c in values if c is not None)
        if any(
            c.observation_id != value.observation_id
            or c.source != p.source
            or c.dataset != p.dataset
            or c.known_from_ns != batch.ingested_at_ns
            for c in claims
        ):
            reasons.add("CLAIM_SOURCE_ITEM_MISMATCH")
        if any(sum(c.item == item for c in claims) != 1 for item in batch.items):
            reasons.add("INCOMPLETE_SOURCE_ITEM")
        for c in claims:
            if c.item not in batch.items:
                old = repository.claim_revision(c.previous_revision_id or "")
                if (
                    batch.snapshot is None
                    or old is None
                    or c.item.revision_kind is not ClaimRevisionKind.TERMINATION
                    or GraphQuality.IMPLIED_TERMINATION not in c.quality
                    or c.item.valid_to_ns != batch.snapshot.effective_ns
                    or c.edge_id != old.edge_id
                    or c.item
                    != replace(
                        old.item,
                        valid_to_ns=batch.snapshot.effective_ns,
                        revision_kind=ClaimRevisionKind.TERMINATION,
                    )
                ):
                    reasons.add("INVALID_IMPLIED_TERMINATION")
        at = (
            batch.snapshot.effective_ns
            if batch.snapshot is not None
            else batch.source_observed_at_ns
        )
        edges = tuple(
            e
            for identity in sorted({c.edge_id for c in claims if c.edge_id is not None})
            for e in resolve_edges(
                repository.current_claims(batch.ingested_at_ns, edge_id=identity),
                batch.ingested_at_ns,
                at,
                value.policy,
            )
        )
        if (
            tuple(sorted(e.revision_id for e in edges)) != value.canonical_revision_ids
            or value.unresolved_count != sum(c.edge_id is None for c in claims)
            or value.conflict_count != sum(bool(e.conflict_reasons) for e in edges)
        ):
            reasons.add("INGESTION_RESOLUTION_OR_COUNTS_MISMATCH")
    except EIEError:
        reasons.add("INVALID_INGESTION_HISTORY")
    return GraphValidationResult(not reasons, tuple(sorted(reasons)))


@dataclass(frozen=True, slots=True)
class GraphValidationResult:
    valid: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        contract(self)


def validate_snapshot(
    value: GraphSnapshot, reader: GraphReader | None = None
) -> GraphValidationResult:
    reasons = set()
    nodes = {n.node_id: n for n in value.nodes}
    edges = {e.edge_id: e for e in value.edges}
    if any(r not in nodes for r in value.roots):
        reasons.add("MISSING_ROOT")
    if any(n.known_from_ns > value.known_as_of_ns for n in value.nodes):
        reasons.add("FUTURE_NODE")
    for edge in value.edges:
        if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
            reasons.add("MISSING_ENDPOINT")
        if (
            not edge.active
            or any(c.known_from_ns > value.known_as_of_ns for c in edge.claims)
            or not claim_active(edge.selected_claim, value.valid_at_ns)
        ):
            reasons.add("INVALID_EDGE_TIME")
        if resolve_edges(edge.claims, value.known_as_of_ns, value.valid_at_ns, value.policy) != (
            edge,
        ):
            reasons.add("EDGE_RESOLUTION_MISMATCH")
    for path in value.paths:
        if (
            path.nodes[0] not in value.roots
            or path.depth > value.traversal.max_depth
            or any(edges.get(e.edge_id) != e for e in path.edges)
        ):
            reasons.add("PATH_MEMBERSHIP_MISMATCH")
        if make_path(path.nodes, path.edges, value.traversal.derive_ownership) != path:
            reasons.add("PATH_EXPOSURE_MISMATCH")
        for i, edge in enumerate(path.edges):
            if edge.relationship_type not in {
                canonical_type(k) for k in value.traversal.relationship_types
            }:
                reasons.add("PATH_RELATIONSHIP_FILTER")
            if edge.relationship_type not in SYMMETRIC and (
                value.traversal.direction is Direction.OUTGOING
                and edge.source_node_id != path.nodes[i]
                or value.traversal.direction is Direction.INCOMING
                and edge.target_node_id != path.nodes[i]
            ):
                reasons.add("PATH_DIRECTION_MISMATCH")
    if reader is not None:
        try:
            if (
                traverse(
                    reader,
                    value.roots,
                    value.known_as_of_ns,
                    value.valid_at_ns,
                    value.traversal,
                    value.policy,
                )
                != value
            ):
                reasons.add("HISTORICAL_RECALCULATION_MISMATCH")
        except EIEError:
            reasons.add("HISTORICAL_INPUT_INVALID")
    return GraphValidationResult(not reasons, tuple(sorted(reasons)))


def validate_propagation(
    value: PropagationResult, reader: GraphReader | None = None
) -> GraphValidationResult:
    reasons = set()
    if value.snapshot is None:
        if (
            value.bindings
            or value.candidates
            or value.event.identity.scope.kind.value
            not in ("MACRO_GLOBAL", "MACRO_COUNTRY", "MARKET", "SEC_FILER")
        ):
            reasons.add("INVALID_UNEXPANDED_SCOPE")
    else:
        snapshot = value.snapshot
        checked = validate_snapshot(snapshot, reader)
        reasons.update(checked.reasons)
        if (
            snapshot.known_as_of_ns != value.known_as_of_ns
            or snapshot.valid_at_ns != value.valid_at_ns
            or snapshot.roots != value.direct_entities
            or candidates_for(value.event, snapshot) != value.candidates
            or value.reasons != snapshot.truncation_reasons
        ):
            reasons.add("PROPAGATION_CUTOFF_OR_CANDIDATE_MISMATCH")
        if reader is not None:
            try:
                if (
                    event_roots(
                        value.event,
                        reader,
                        value.known_as_of_ns,
                        value.valid_at_ns,
                        exchange=value.bindings[0].exchange if value.bindings else None,
                    )
                    != value.bindings
                ):
                    reasons.add("EVENT_SCOPE_MISMATCH")
            except EIEError:
                reasons.add("EVENT_SCOPE_INVALID")
        if any(
            b.scope_identifier not in value.event.identity.scope.identifiers for b in value.bindings
        ):
            reasons.add("UNKNOWN_EVENT_SCOPE_BINDING")
        for binding in value.bindings:
            if binding.method == "EXACT_HISTORICAL_SYMBOL":
                n = binding.listing
                f = n.foundation if n is not None else None
                if (
                    n is None
                    or not isinstance(f, SecListingRevision)
                    or n.known_from_ns > value.known_as_of_ns
                    or f.issuer_id != binding.node_id
                    or f.ticker != binding.scope_identifier
                    or f.exchange != binding.exchange
                    or f.valid_from_ns > value.valid_at_ns
                    or f.valid_to_ns is not None
                    and value.valid_at_ns >= f.valid_to_ns
                ):
                    reasons.add("INVALID_SYMBOL_RESOLUTION_EVIDENCE")
            elif (
                binding.method != "EXACT_SCOPE_NODE"
                or binding.listing is not None
                or binding.exchange is not None
                or binding.node_id != binding.scope_identifier
            ):
                reasons.add("INVALID_DIRECT_SCOPE_BINDING")
    return GraphValidationResult(not reasons, tuple(sorted(reasons)))


def validate_hierarchy(reader: GraphReader, known_as_of_ns: int, policy: GraphPolicy) -> None:
    claims = reader.current_claims(
        known_as_of_ns, relationship_types=(RelationshipType.PARENT_OF,), limit=100000
    )
    if len(claims) > 10000:
        raise ValidationError("parent hierarchy validation bound exceeded")
    boundaries = sorted(
        {t for c in claims for t in (c.item.valid_from_ns, c.item.valid_to_ns) if t is not None}
    )
    if len(boundaries) > 10000:
        raise ValidationError("parent validity partition bound exceeded")
    for valid_at in boundaries:
        adjacency: dict[str, set[str]] = {}
        for edge in resolve_edges(claims, known_as_of_ns, valid_at, policy):
            if edge.active:
                adjacency.setdefault(edge.source_node_id, set()).add(edge.target_node_id)
        done = set()
        for root in sorted(adjacency):
            if root in done:
                continue
            active: set[str] = set()
            stack = [(root, False)]
            while stack:
                node, exiting = stack.pop()
                if exiting:
                    active.remove(node)
                    done.add(node)
                    continue
                if node in active:
                    raise ValidationError("parent hierarchy cycle at historical valid time")
                if node in done:
                    continue
                active.add(node)
                stack.append((node, True))
                stack.extend((n, False) for n in sorted(adjacency.get(node, ()), reverse=True))
