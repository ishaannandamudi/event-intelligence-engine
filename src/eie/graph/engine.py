"""Raw-first atomic ingestion, explicit snapshot transitions and hierarchy checks."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.entities.protocols import ResolutionState
from eie.entities.sec import SecEntityRegistry
from eie.graph.claims import (
    ClaimRevisionKind,
    Endpoint,
    GraphQuality,
    IdentifierKind,
    ProviderRelationshipClaim,
    ValidSemantics,
)
from eie.graph.models import DEFAULT_GRAPH_POLICY, GraphPolicy
from eie.graph.taxonomy import RelationshipType
from eie.providers.relationships.adapter import RelationshipAdapter
from eie.providers.relationships.models import GraphIngestionResult, SnapshotPolicy
from eie.sources.models import RawSourceObservation

if TYPE_CHECKING:
    from eie.graph.repository import GraphRepository


class GraphIngestor:
    def __init__(
        self,
        repository: GraphRepository,
        policy: GraphPolicy = DEFAULT_GRAPH_POLICY,
        registry: SecEntityRegistry | None = None,
    ) -> None:
        self.repository, self.policy, self.registry = repository, policy, registry

    def resolve(self, endpoint: Endpoint, known_as_of_ns: int, valid_at_ns: int) -> str | None:
        if endpoint.kind is IdentifierKind.NODE_ID:
            node = self.repository.node(endpoint.value, known_as_of_ns)
        elif endpoint.kind is IdentifierKind.SYMBOL:
            assert endpoint.exchange is not None
            from eie.entities.sec import SecListingRevision

            listings = self.repository.symbol_listings(
                endpoint.value, endpoint.exchange, known_as_of_ns, valid_at_ns
            )
            identities = {
                v.foundation.issuer_id
                for v in listings
                if isinstance(v.foundation, SecListingRevision)
            }
            if len(identities) != 1:
                return None
            identity = next(iter(identities))
            if self.registry is not None:
                result = self.registry.resolve_symbol(
                    endpoint.value, endpoint.exchange, known_as_of_ns, valid_at_ns=valid_at_ns
                )
                if result.state is not ResolutionState.RESOLVED or result.candidate_ids != (
                    identity,
                ):
                    return None
            node = self.repository.node(identity, known_as_of_ns)
        else:
            authority = "SEC_CIK" if endpoint.kind is IdentifierKind.CIK else endpoint.authority
            assert authority is not None
            matches = self.repository.nodes_by_identifier(
                authority, endpoint.value, known_as_of_ns, endpoint.expected_type
            )
            node = matches[0] if len(matches) == 1 else None
        return (
            node.node_id if node is not None and node.node_type is endpoint.expected_type else None
        )

    def ingest(self, adapter: RelationshipAdapter, payload: bytes) -> GraphIngestionResult:
        from eie.graph.validation import validate_hierarchy
        from eie.providers.relationships.normalization import validate_endpoints, validate_exposure

        batch = adapter.parse(payload)
        p = adapter.provider
        batch_id = "graph_batch_" + fingerprint(
            "eie:graph-ingestion-identity:v1", (p, batch, self.policy)
        )
        previous = self.repository.batch(batch_id)
        if previous is not None:
            return previous
        raw = RawSourceObservation(
            p.source,
            batch.source_item_id,
            batch.source_item_version,
            sha256(payload).hexdigest(),
            "application/vnd.eie.relationships+json",
            batch.source_observed_at_ns,
            batch.ingested_at_ns,
            batch.published_at_ns,
            batch.source_reference,
            "STRUCTURED_RELATIONSHIPS",
            tuple(
                sorted(
                    {e.value for i in batch.items for e in (i.source_endpoint, i.target_endpoint)}
                )
            ),
            tuple(
                sorted(
                    (
                        ("dataset_id", p.dataset.dataset_id),
                        ("dataset_fingerprint", p.dataset.fingerprint),
                        ("dataset_version", p.dataset.version),
                        ("parser_version", p.parser_version),
                        ("resource_id", batch.resource_id),
                        ("snapshot_id", batch.snapshot_id),
                    )
                )
            ),
        )
        with self.repository.store.transaction():
            last = self.repository.last_batch(p.source.name, batch.resource_id)
            if last is not None and batch.ingested_at_ns <= last.batch.ingested_at_ns:
                raise ValidationError("source resource ingestion knowledge must advance")
            self.repository.store.append_observation(raw)
            claims = []
            owner = None
            if batch.snapshot is not None:
                owner = self.resolve(
                    batch.snapshot.owner, batch.ingested_at_ns, batch.snapshot.effective_ns
                )
                if owner is None:
                    raise ValidationError("composition snapshot owner must resolve exactly")
            for item in batch.items:
                at = (
                    batch.source_observed_at_ns
                    if item.valid_from_ns is None
                    else item.valid_from_ns
                )
                source = self.resolve(item.source_endpoint, batch.ingested_at_ns, at)
                target = self.resolve(item.target_endpoint, batch.ingested_at_ns, at)
                flags = set(item.quality)
                if source is None or target is None:
                    flags.add(GraphQuality.UNRESOLVED_ENTITY)
                if item.valid_from_ns is None:
                    flags.add(GraphQuality.UNKNOWN_VALID_FROM)
                key = item.claim_key
                if batch.snapshot is not None:
                    expected_owner = (
                        source
                        if batch.snapshot.relationship_type is RelationshipType.ETF_HOLDS
                        else target
                    )
                    if expected_owner != owner:
                        raise ValidationError("composition row belongs to another owner")
                    key = "episode_" + fingerprint(
                        "eie:composition-episode:v1", (item.claim_key, batch.snapshot.effective_ns)
                    )
                    if not batch.snapshot.exhaustive:
                        flags.add(GraphQuality.PARTIAL_SNAPSHOT)
                    if p.snapshot_policy is SnapshotPolicy.CARRY_UNTIL_NEXT:
                        flags.add(GraphQuality.CARRIED_SNAPSHOT)
                history = self.repository.claim_history_for_key(
                    p.source.name, batch.resource_id, key
                )
                old = history[-1] if history else None
                claim = ProviderRelationshipClaim(
                    p.source,
                    p.dataset,
                    batch.resource_id,
                    key,
                    item,
                    raw.observation_id,
                    1 if old is None else old.number + 1,
                    batch.ingested_at_ns,
                    source,
                    target,
                    None if old is None else old.revision_id,
                    frozenset(flags),
                    resolution_valid_at_ns=at,
                )
                validate_endpoints(claim, self.repository)
                validate_exposure(claim)
                claims.append(claim)
            if batch.snapshot is not None and p.snapshot_policy is SnapshotPolicy.CARRY_UNTIL_NEXT:
                if batch.snapshot.remove_missing and any(c.edge_id is None for c in claims):
                    raise ValidationError("unresolved exhaustive composition cannot imply removal")
                assert owner is not None
                existing = self.repository.current_claims(
                    batch.ingested_at_ns,
                    node_id=owner,
                    relationship_types=(batch.snapshot.relationship_type,),
                    provider=p.source.name,
                    limit=100000,
                )
                present = {c.edge_id for c in claims}
                for old in existing:
                    if (
                        old.item.valid_semantics is not ValidSemantics.INTERVAL
                        or old.resource_id != batch.resource_id
                        or old.item.valid_to_ns is not None
                        or old.item.valid_from_ns is None
                        or (
                            old.endpoints[1]
                            if batch.snapshot.relationship_type is RelationshipType.ETF_HOLDS
                            else old.endpoints[2]
                        )
                        != owner
                    ):
                        continue
                    if old.item.valid_from_ns >= batch.snapshot.effective_ns:
                        raise ValidationError(
                            "carry snapshots must advance in valid time; use explicit correction"
                        )
                    if old.edge_id not in present and not batch.snapshot.remove_missing:
                        continue
                    closed = replace(
                        old,
                        item=replace(
                            old.item,
                            valid_to_ns=batch.snapshot.effective_ns,
                            revision_kind=ClaimRevisionKind.TERMINATION,
                        ),
                        observation_id=raw.observation_id,
                        number=old.number + 1,
                        known_from_ns=batch.ingested_at_ns,
                        previous_revision_id=old.revision_id,
                        source=p.source,
                        dataset=p.dataset,
                        quality=old.quality | frozenset({GraphQuality.IMPLIED_TERMINATION}),
                    )
                    claims.append(closed)
            for claim in sorted(claims, key=lambda c: (c.claim_id, c.number)):
                self.repository.append_claim(claim)
            if any(c.endpoints[0] is RelationshipType.PARENT_OF for c in claims):
                validate_hierarchy(self.repository, batch.ingested_at_ns, self.policy)
            at = (
                batch.snapshot.effective_ns
                if batch.snapshot is not None
                else batch.source_observed_at_ns
            )
            canonical = self.repository.materialize_edges(
                tuple(sorted({c.edge_id for c in claims if c.edge_id is not None})),
                batch.ingested_at_ns,
                at,
                self.policy,
            )
            result = GraphIngestionResult(
                batch_id,
                raw.observation_id,
                p,
                batch,
                tuple(sorted(c.revision_id for c in claims)),
                tuple(sorted(e.revision_id for e in canonical)),
                1 if last is None else last.checkpoint_generation + 1,
                sum(c.edge_id is None for c in claims),
                sum(bool(e.conflict_reasons) for e in canonical),
                self.policy,
            )
            self.repository.append_batch(result)
            return result
