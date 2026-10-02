"""Immutable SQLite graph history, indexed local adjacency and sealed artifacts."""

from typing import TypeVar

from eie.core.errors import PersistenceError, ValidationError
from eie.core.time import require_ns
from eie.graph.claims import ProviderRelationshipClaim
from eie.graph.history import (
    ClaimKnowledgeInterval,
    GraphHistoryQuery,
    GraphLimitError,
    resolve_edges,
)
from eie.graph.models import GraphPolicy, GraphSnapshot, RelationshipEdge
from eie.graph.nodes import GraphNode
from eie.graph.propagation import PropagationResult
from eie.graph.taxonomy import GraphNodeType, RelationshipType, canonical_type
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.relationships.models import GraphIngestionResult

T = TypeVar("T")


def bounded_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000:
        raise ValidationError("bounded graph claim query required")


def typed(payload: object, cls: type[T]) -> T:
    if not isinstance(payload, bytes):
        raise PersistenceError("graph payload bytes required")
    value = codec.loads(payload)
    if not isinstance(value, cls):
        raise PersistenceError("graph stored contract type mismatch")
    return value


class GraphRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def _knowledge_append(self, known_ns: int) -> None:
        clocks = self.store._rows(
            "SELECT (SELECT MAX(known) FROM graph_nodes),(SELECT MAX(known) FROM graph_claims),"
            "(SELECT MAX(known) FROM graph_batches)"
        )[0]
        latest = max((v for v in clocks if isinstance(v, int)), default=None)
        sealed = self.store._rows("SELECT MAX(known) FROM graph_snapshots")[0][0]
        if sealed is not None and not isinstance(sealed, int):
            raise PersistenceError("invalid sealed graph knowledge clock")
        completed = clocks[2]
        if (
            latest is not None
            and known_ns < latest
            or isinstance(completed, int)
            and known_ns <= completed
            or sealed is not None
            and known_ns <= sealed
        ):
            raise ValidationError(
                "graph knowledge must advance past completed items and sealed cutoffs"
            )

    def _dataset(self, node_or_claim: GraphNode | ProviderRelationshipClaim) -> None:
        from eie.datasets.repository import DatasetRepository

        dataset = node_or_claim.dataset
        if dataset.build_id is not None:
            manifest = DatasetRepository(self.store).manifest(dataset.build_id)
            if manifest is None or manifest.content_fingerprint != dataset.fingerprint:
                raise ValidationError("graph historical build fingerprint disagrees")

    def append_node(self, value: GraphNode) -> bool:
        from eie.entities.models import Security
        from eie.entities.sec import SecListingRevision

        with self.store.transaction():
            old = self.node(value.node_id, 2**63 - 1)
            if old == value:
                return False
            self._knowledge_append(value.known_from_ns)
            self._dataset(value)
            if old is not None and (
                value.known_from_ns <= old.known_from_ns or old.node_type is not value.node_type
            ):
                raise ValidationError("node identity type is stable and knowledge must increase")
            if (
                old is not None
                and value.node_type is GraphNodeType.ISSUER
                and tuple(i for i in old.identifiers if i.authority == "SEC_CIK")
                != tuple(i for i in value.identifiers if i.authority == "SEC_CIK")
            ):
                raise ValidationError("verified issuer CIK cannot be rebound")
            f = value.foundation
            if isinstance(f, Security):
                issuer = self.node(f.issuer_id, value.known_from_ns)
                if issuer is None or issuer.node_type is not GraphNodeType.ISSUER:
                    raise ValidationError("security needs known verified issuer node")
                if (
                    isinstance(old.foundation if old is not None else None, Security)
                    and old is not None
                    and old.foundation != f
                ):
                    raise ValidationError("verified security identity cannot be rebound")
            if isinstance(f, SecListingRevision):
                security = self.node(f.security_id, value.known_from_ns)
                if (
                    security is None
                    or not isinstance(security.foundation, Security)
                    or security.foundation.issuer_id != f.issuer_id
                ):
                    raise ValidationError("listing needs verified security/issuer link")
            if old is None:
                self.store._connection.execute(
                    "INSERT INTO graph_entities VALUES (?,?)",
                    (value.node_id, value.node_type.value),
                )
            self.store._connection.execute(
                "INSERT INTO graph_nodes VALUES (?,?,?,?,?)",
                (
                    value.revision_id,
                    value.node_id,
                    value.known_from_ns,
                    value.dataset.fingerprint,
                    codec.dumps(value),
                ),
            )
            self.store._connection.executemany(
                "INSERT INTO graph_identifiers VALUES (?,?,?,?)",
                (
                    (value.revision_id, i.authority, i.value, value.node_id)
                    for i in value.identifiers
                ),
            )
            if isinstance(f, SecListingRevision):
                self.store._connection.execute(
                    "INSERT INTO graph_listing_index VALUES (?,?,?,?,?,?,?)",
                    (
                        value.revision_id,
                        f.listing_id,
                        f.ticker,
                        f.exchange,
                        f.issuer_id,
                        f.valid_from_ns,
                        f.valid_to_ns,
                    ),
                )
            return True

    def symbol_listings(
        self, ticker: str, exchange: str, known_as_of_ns: int, valid_at_ns: int
    ) -> tuple[GraphNode, ...]:
        from eie.entities.sec import EXCHANGE, TICKER, SecListingRevision

        require_ns(known_as_of_ns)
        require_ns(valid_at_ns)
        if not TICKER.fullmatch(ticker) or not EXCHANGE.fullmatch(exchange):
            raise ValidationError("exact historical ticker/exchange required")
        rows = self.store._rows(
            "SELECT l.node FROM graph_listing_index l JOIN graph_nodes n ON n.id=l.revision "
            "WHERE l.ticker=? AND l.exchange=? AND n.known<=? "
            "AND l.valid_from<=? AND (l.valid_to IS NULL OR ?<l.valid_to) "
            "AND NOT EXISTS(SELECT 1 FROM graph_nodes newer WHERE newer.node=n.node "
            "AND newer.known<=? AND newer.known>n.known) ORDER BY l.node LIMIT 101",
            (ticker, exchange, known_as_of_ns, valid_at_ns, valid_at_ns, known_as_of_ns),
        )
        if len(rows) > 100:
            raise GraphLimitError("historical symbol ambiguity exceeds bound")
        values = tuple(self.node(str(r[0]), known_as_of_ns) for r in rows)
        if any(
            v is None
            or not isinstance(v.foundation, SecListingRevision)
            or v.foundation.ticker != ticker
            or v.foundation.exchange != exchange
            or v.foundation.valid_from_ns > valid_at_ns
            or v.foundation.valid_to_ns is not None
            and valid_at_ns >= v.foundation.valid_to_ns
            for v in values
        ):
            raise PersistenceError("historical listing index disagrees")
        return tuple(v for v in values if v is not None)

    def node(self, node_id: str, known_as_of_ns: int) -> GraphNode | None:
        require_ns(known_as_of_ns)
        rows = self.store._rows(
            (
                "SELECT n.id,n.node,n.known,n.dataset,n.payload,e.kind FROM graph_"
                "nodes n JOIN graph_entities e ON e.id=n.node WHERE n.node=? AND n"
                ".known<=? ORDER BY n.known DESC LIMIT 1"
            ),
            (node_id, known_as_of_ns),
        )
        if not rows:
            return None
        row = rows[0]
        value = typed(row[4], GraphNode)
        if (
            row[:4]
            != (value.revision_id, value.node_id, value.known_from_ns, value.dataset.fingerprint)
            or row[5] != value.node_type.value
            or value.node_id != node_id
            or value.known_from_ns > known_as_of_ns
        ):
            raise PersistenceError("graph node index/identity disagrees")
        identifiers = self.store._rows(
            (
                "SELECT authority,value,node FROM graph_identifiers WHERE revision"
                "=? ORDER BY authority,value"
            ),
            (value.revision_id,),
        )
        if identifiers != [(i.authority, i.value, value.node_id) for i in value.identifiers]:
            raise PersistenceError("graph identifier mapping disagrees")
        from eie.entities.sec import SecListingRevision

        if isinstance(value.foundation, SecListingRevision):
            f = value.foundation
            if self.store._rows(
                "SELECT * FROM graph_listing_index WHERE revision=?", (value.revision_id,)
            ) != [
                (
                    value.revision_id,
                    value.node_id,
                    f.ticker,
                    f.exchange,
                    f.issuer_id,
                    f.valid_from_ns,
                    f.valid_to_ns,
                )
            ]:
                raise PersistenceError("verified listing mapping disagrees")
        return value

    def nodes_by_identifier(
        self, authority: str, value: str, known_as_of_ns: int, node_type: GraphNodeType
    ) -> tuple[GraphNode, ...]:
        require_ns(known_as_of_ns)
        rows = self.store._rows(
            (
                "SELECT DISTINCT i.node FROM graph_identifiers i JOIN graph_nodes "
                "n ON n.id=i.revision JOIN graph_entities e ON e.id=n.node WHERE i"
                ".authority=? AND i.value=? AND e.kind=? AND n.known<=? AND NOT EX"
                "ISTS(SELECT 1 FROM graph_nodes newer WHERE newer.node=n.node AND "
                "newer.known<=? AND newer.known>n.known) ORDER BY i.node LIMIT 101"
            ),
            (authority, value, node_type.value, known_as_of_ns, known_as_of_ns),
        )
        if len(rows) > 100:
            raise GraphLimitError("identifier ambiguity exceeds bound")
        nodes = tuple(self.node(str(r[0]), known_as_of_ns) for r in rows)
        if any(
            n is None
            or n.node_type is not node_type
            or not any(i.authority == authority and i.value == value for i in n.identifiers)
            for n in nodes
        ):
            raise PersistenceError("identifier index inconsistent with node")
        return tuple(n for n in nodes if n is not None)

    @staticmethod
    def claim_indexes(value: ProviderRelationshipClaim) -> tuple[object, ...]:
        kind, source, target = value.endpoints
        return (
            value.revision_id,
            value.claim_id,
            value.number,
            value.previous_revision_id,
            value.edge_id,
            value.source.name,
            value.resource_id,
            value.known_from_ns,
            value.item.valid_from_ns,
            value.item.valid_to_ns,
            value.item.valid_semantics.value,
            kind.value,
            source,
            target,
            None if value.item.exposure is None else value.item.exposure.measurement_type.value,
            value.observation_id,
        )

    def _claim(self, row: tuple[object, ...]) -> ProviderRelationshipClaim:
        from eie.providers.relationships.normalization import validate_endpoints, validate_exposure

        value = typed(row[-1], ProviderRelationshipClaim)
        if row[:-1] != self.claim_indexes(value):
            raise PersistenceError("graph claim indexes/fingerprint disagree")
        raw = self.store.get_observation(value.observation_id)
        if (
            raw is None
            or raw.source != value.source
            or raw.ingested_at_ns != value.known_from_ns
            or dict(raw.metadata).get("resource_id") != value.resource_id
            or dict(raw.metadata).get("dataset_fingerprint") != value.dataset.fingerprint
            or dict(raw.metadata).get("dataset_id") != value.dataset.dataset_id
            or dict(raw.metadata).get("dataset_version") != value.dataset.version
        ):
            raise PersistenceError("graph claim raw provenance disagrees")
        if value.previous_revision_id is not None:
            previous = self.store._rows(
                "SELECT claim_id,number,known FROM graph_claims WHERE id=?",
                (value.previous_revision_id,),
            )
            if not previous or previous[0][:2] != (value.claim_id, value.number - 1):
                raise PersistenceError("graph predecessor missing or inconsistent")
            if int(str(previous[0][2])) >= value.known_from_ns:
                raise PersistenceError("graph revision knowledge did not advance")
        for identity in (value.source_node_id, value.target_node_id):
            if identity is not None and self.node(identity, value.known_from_ns) is None:
                raise PersistenceError("graph endpoint reference unavailable")
        if value.edge_id is not None:
            kind, source, target = value.endpoints
            if self.store._rows(
                "SELECT kind,source_node,target_node FROM graph_edges WHERE id=?", (value.edge_id,)
            ) != [(kind.value, source, target)]:
                raise PersistenceError("semantic edge index disagrees with provider claim")
        try:
            validate_endpoints(value, self)
            validate_exposure(value)
        except ValidationError as exc:
            raise PersistenceError("claim source endpoint/exposure disagrees") from exc
        return value

    def claim_revision(self, revision_id: str) -> ProviderRelationshipClaim | None:
        rows = self.store._rows("SELECT * FROM graph_claims WHERE id=?", (revision_id,))
        return None if not rows else self._claim(rows[0])

    def claim_history_for_key(
        self, provider: str, resource: str, key: str
    ) -> tuple[ProviderRelationshipClaim, ...]:
        from eie.core.fingerprints import fingerprint

        identity = "claim_" + fingerprint("eie:relationship-claim:v1", (provider, resource, key))
        return self.claim_history(identity)

    def claim_history(
        self, claim_id: str, *, limit: int = 100000
    ) -> tuple[ProviderRelationshipClaim, ...]:
        bounded_limit(limit)
        rows = self.store._rows(
            "SELECT * FROM graph_claims WHERE claim_id=? ORDER BY number LIMIT ?",
            (claim_id, limit + 1),
        )
        if len(rows) > limit:
            raise GraphLimitError("complete claim history exceeds input bound")
        values = tuple(self._claim(r) for r in rows)
        if any(
            v.number != i + 1
            or v.previous_revision_id != (None if i == 0 else values[i - 1].revision_id)
            for i, v in enumerate(values)
        ):
            raise PersistenceError("claim history ordering/chain disagrees")
        return values

    def current_claims(
        self,
        known_as_of_ns: int,
        *,
        node_id: str | None = None,
        edge_id: str | None = None,
        relationship_types: tuple[RelationshipType, ...] = (),
        provider: str | None = None,
        limit: int = 10000,
    ) -> tuple[ProviderRelationshipClaim, ...]:
        require_ns(known_as_of_ns)
        bounded_limit(limit)
        clauses = ["c.known<=?"]
        values: list[object] = [known_as_of_ns]
        if node_id is not None:
            clauses.append("(c.source_node=? OR c.target_node=?)")
            values.extend((node_id, node_id))
        if edge_id is not None:
            clauses.append("c.edge_id=?")
            values.append(edge_id)
        if provider is not None:
            clauses.append("c.provider=?")
            values.append(provider)
        if relationship_types:
            clauses.append("c.kind IN (" + ",".join("?" for _ in relationship_types) + ")")
            values.extend(k.value for k in relationship_types)
        clauses.append(
            "NOT EXISTS(SELECT 1 FROM graph_claims newer WHERE newer.claim_id="
            "c.claim_id AND newer.known<=? AND newer.number>c.number)"
        )
        values.extend((known_as_of_ns, limit + 1))
        rows = self.store._rows(
            "SELECT c.* FROM graph_claims c WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.edge_id,c.claim_id LIMIT ?",
            tuple(values),
        )
        if len(rows) > limit:
            raise GraphLimitError("complete claim groups exceed adjacency input bound")
        claims = tuple(self._claim(r) for r in rows)
        if any(
            c.known_from_ns > known_as_of_ns
            or edge_id is not None
            and c.edge_id != edge_id
            or node_id is not None
            and node_id not in c.endpoints[1:]
            or provider is not None
            and c.source.name != provider
            or relationship_types
            and c.endpoints[0] not in relationship_types
            for c in claims
        ):
            raise PersistenceError("claim query index leaked incompatible/future state")
        return claims

    def search_history(self, query: GraphHistoryQuery) -> tuple[ClaimKnowledgeInterval, ...]:
        # Derive each revision's knowledge end only from revisions visible at the cutoff.
        clauses = ["c.known<=?"]
        values: list[object] = [query.known_as_of_ns, query.known_as_of_ns]
        if query.node_id is not None:
            clauses.append("(c.source_node=? OR c.target_node=?)")
            values.extend((query.node_id, query.node_id))
        if query.provider is not None:
            clauses.append("c.provider=?")
            values.append(query.provider)
        if query.relationship_types:
            kinds = tuple(sorted({canonical_type(k) for k in query.relationship_types}))
            clauses.append("c.kind IN (" + ",".join("?" for _ in kinds) + ")")
            values.extend(k.value for k in kinds)
        if query.exposure_type is not None:
            clauses.append("c.exposure_type=?")
            values.append(query.exposure_type.value)
        if query.valid_interval is not None:
            start, end = query.valid_interval
            clauses.append(
                "((c.semantics='INTERVAL' AND c.valid_from<? "
                "AND (c.valid_to IS NULL OR c.valid_to>?)) OR "
                "(c.semantics='SNAPSHOT' AND c.valid_from>=? AND c.valid_from<?))"
            )
            values.extend((end, start, start, end))
        outer = ""
        if query.known_interval is not None:
            start, end = query.known_interval
            outer = " WHERE known<? AND (next_known IS NULL OR next_known>?)"
            values.extend((end, start))
        values.append(query.limit + 1)
        rows = self.store._rows(
            "SELECT * FROM (SELECT c.*, (SELECT MIN(newer.known) FROM graph_claims newer "
            "WHERE newer.claim_id=c.claim_id AND newer.number>c.number AND newer.known<=?) "
            "AS next_known FROM graph_claims c WHERE "
            + " AND ".join(clauses)
            + ")"
            + outer
            + " ORDER BY edge_id,claim_id,number LIMIT ?",
            tuple(values),
        )
        if len(rows) > query.limit:
            raise GraphLimitError("complete history search exceeds input bound")
        result = []
        for row in rows:
            claim = self._claim(row[:-1])
            end_ns = row[-1]
            if end_ns is not None and (
                not isinstance(end_ns, int) or end_ns <= claim.known_from_ns
            ):
                raise PersistenceError("invalid derived knowledge interval")
            result.append(ClaimKnowledgeInterval(claim, end_ns))
        return tuple(result)

    def append_claim(self, value: ProviderRelationshipClaim) -> bool:
        from eie.providers.relationships.normalization import validate_endpoints, validate_exposure

        with self.store.transaction():
            if self.claim_revision(value.revision_id) == value:
                return False
            self._knowledge_append(value.known_from_ns)
            self._dataset(value)
            history = self.claim_history(value.claim_id)
            old = history[-1] if history else None
            if (
                value.number != len(history) + 1
                or value.previous_revision_id != (None if old is None else old.revision_id)
                or old is not None
                and value.known_from_ns <= old.known_from_ns
            ):
                raise ValidationError("ordered immutable provider claim chain required")
            raw = self.store.get_observation(value.observation_id)
            if (
                raw is None
                or raw.source != value.source
                or raw.ingested_at_ns != value.known_from_ns
                or dict(raw.metadata).get("resource_id") != value.resource_id
                or dict(raw.metadata).get("dataset_fingerprint") != value.dataset.fingerprint
                or dict(raw.metadata).get("dataset_id") != value.dataset.dataset_id
                or dict(raw.metadata).get("dataset_version") != value.dataset.version
            ):
                raise ValidationError("raw relationship observation must be committed first")
            validate_endpoints(value, self)
            validate_exposure(value)
            if value.edge_id is not None:
                kind, source, target = value.endpoints
                existing = self.store._rows(
                    "SELECT kind,source_node,target_node FROM graph_edges WHERE id=?",
                    (value.edge_id,),
                )
                if existing and existing != [(kind.value, source, target)]:
                    raise PersistenceError("canonical edge identity collision")
                if not existing:
                    self.store._connection.execute(
                        "INSERT INTO graph_edges VALUES (?,?,?,?)",
                        (value.edge_id, kind.value, source, target),
                    )
            self.store._connection.execute(
                "INSERT INTO graph_claims VALUES (" + ",".join("?" for _ in range(17)) + ")",
                (*self.claim_indexes(value), codec.dumps(value)),
            )
            return True

    def relationship_history(
        self, edge_id: str, known_as_of_ns: int, *, limit: int = 100000
    ) -> tuple[ProviderRelationshipClaim, ...]:
        require_ns(known_as_of_ns)
        bounded_limit(limit)
        rows = self.store._rows(
            (
                "SELECT * FROM graph_claims WHERE edge_id=? AND known<=? ORDER BY "
                "known,claim_id,number LIMIT ?"
            ),
            (edge_id, known_as_of_ns, limit + 1),
        )
        if len(rows) > limit:
            raise GraphLimitError("complete relationship history exceeds input bound")
        return tuple(self._claim(r) for r in rows)

    def materialize_edges(
        self, edge_ids: tuple[str, ...], known_as_of_ns: int, valid_at_ns: int, policy: GraphPolicy
    ) -> tuple[RelationshipEdge, ...]:
        edges = tuple(
            e
            for identity in edge_ids
            for e in resolve_edges(
                self.current_claims(known_as_of_ns, edge_id=identity),
                known_as_of_ns,
                valid_at_ns,
                policy,
            )
        )
        for edge in edges:
            self._append_edge(edge)
        return edges

    def _append_edge(self, value: RelationshipEdge) -> None:
        old = self.edge_revision(value.revision_id)
        if old is not None:
            if old != value:
                raise PersistenceError("immutable edge revision collision")
            return
        self.store._connection.execute(
            "INSERT INTO graph_edge_revisions VALUES (?,?,?,?,?,?)",
            (
                value.revision_id,
                value.edge_id,
                value.known_from_ns,
                int(value.active),
                None if value.exposure is None else value.exposure.measurement_type.value,
                codec.dumps(value),
            ),
        )

    def edge_revision(self, revision_id: str) -> RelationshipEdge | None:
        rows = self.store._rows("SELECT * FROM graph_edge_revisions WHERE id=?", (revision_id,))
        if not rows:
            return None
        row = rows[0]
        value = typed(row[-1], RelationshipEdge)
        if row[:-1] != (
            value.revision_id,
            value.edge_id,
            value.known_from_ns,
            int(value.active),
            None if value.exposure is None else value.exposure.measurement_type.value,
        ) or any(self.claim_revision(c.revision_id) != c for c in value.claims):
            raise PersistenceError("canonical revision index/claim reference disagrees")
        return value

    def batch(self, batch_id: str) -> GraphIngestionResult | None:
        from eie.graph.validation import validate_ingestion

        rows = self.store._rows("SELECT * FROM graph_batches WHERE id=?", (batch_id,))
        if not rows:
            return None
        row = rows[0]
        value = typed(row[-1], GraphIngestionResult)
        if (
            row[:-1]
            != (
                value.batch_id,
                value.provider.source.name,
                value.batch.resource_id,
                value.batch.ingested_at_ns,
                value.batch.snapshot_id,
                value.observation_id,
                value.checkpoint_generation,
                value.result_fingerprint,
            )
            or self.store.get_observation(value.observation_id) is None
            or any(self.claim_revision(i) is None for i in value.claim_revision_ids)
            or any(self.edge_revision(i) is None for i in value.canonical_revision_ids)
        ):
            raise PersistenceError("graph ingestion indexes/references disagree")
        if not validate_ingestion(value, self).valid:
            raise PersistenceError("graph source-item normalization/counts disagree")
        return value

    def last_batch(self, provider: str, resource: str) -> GraphIngestionResult | None:
        rows = self.store._rows(
            (
                "SELECT id FROM graph_batches WHERE provider=? AND resource=? ORDE"
                "R BY generation DESC LIMIT 1"
            ),
            (provider, resource),
        )
        return None if not rows else self.batch(str(rows[0][0]))

    def append_batch(self, value: GraphIngestionResult) -> None:
        from eie.graph.validation import validate_ingestion

        with self.store.transaction():
            self._knowledge_append(value.batch.ingested_at_ns)
            if not validate_ingestion(value, self).valid:
                raise ValidationError("graph ingestion evidence/normalization/counts disagree")
            self.store._connection.execute(
                "INSERT INTO graph_batches VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    value.batch_id,
                    value.provider.source.name,
                    value.batch.resource_id,
                    value.batch.ingested_at_ns,
                    value.batch.snapshot_id,
                    value.observation_id,
                    value.checkpoint_generation,
                    value.result_fingerprint,
                    codec.dumps(value),
                ),
            )

    def append_snapshot(self, value: GraphSnapshot) -> bool:
        from eie.graph.validation import validate_snapshot

        with self.store.transaction():
            if not validate_snapshot(value, self).valid or len(codec.dumps(value)) > 50_000_000:
                raise ValidationError("valid bounded immutable graph snapshot required")
            old = self.snapshot(value.snapshot_id)
            if old is not None:
                if old != value:
                    raise PersistenceError("immutable graph snapshot collision")
                return False
            for edge in value.edges:
                self._append_edge(edge)
            self.store._connection.execute(
                "INSERT INTO graph_snapshots VALUES (?,?,?,?,?)",
                (
                    value.snapshot_id,
                    value.known_as_of_ns,
                    value.valid_at_ns,
                    value.result_fingerprint,
                    codec.dumps(value),
                ),
            )
            self.store._connection.executemany(
                "INSERT INTO graph_snapshot_edges VALUES (?,?)",
                ((value.snapshot_id, e.revision_id) for e in value.edges),
            )
            self.store._connection.executemany(
                "INSERT INTO graph_snapshot_nodes VALUES (?,?)",
                ((value.snapshot_id, n.revision_id) for n in value.nodes),
            )
            return True

    def snapshot(self, snapshot_id: str) -> GraphSnapshot | None:
        from eie.graph.validation import validate_snapshot

        rows = self.store._rows("SELECT * FROM graph_snapshots WHERE id=?", (snapshot_id,))
        if not rows:
            return None
        row = rows[0]
        value = typed(row[-1], GraphSnapshot)
        if (
            row[:-1]
            != (
                value.snapshot_id,
                value.known_as_of_ns,
                value.valid_at_ns,
                value.result_fingerprint,
            )
            or not validate_snapshot(value, self).valid
            or self.store._rows(
                "SELECT revision FROM graph_snapshot_edges WHERE snapshot=? ORDER BY revision",
                (snapshot_id,),
            )
            != [(k,) for k in sorted(e.revision_id for e in value.edges)]
            or self.store._rows(
                "SELECT revision FROM graph_snapshot_nodes WHERE snapshot=? ORDER BY revision",
                (snapshot_id,),
            )
            != [(k,) for k in sorted(n.revision_id for n in value.nodes)]
        ):
            raise PersistenceError("snapshot indexes/membership/recalculation disagree")
        return value

    def append_propagation(self, value: PropagationResult) -> bool:
        from eie.graph.validation import validate_propagation

        with self.store.transaction():
            if not validate_propagation(value, self).valid or len(codec.dumps(value)) > 50_000_000:
                raise ValidationError("valid bounded propagation artifact required")
            old = self.propagation(value.result_id)
            if old is not None:
                if old != value:
                    raise PersistenceError("immutable propagation collision")
                return False
            if value.snapshot is not None:
                self.append_snapshot(value.snapshot)
            self.store._connection.execute(
                "INSERT INTO graph_propagation VALUES (?,?,?,?)",
                (
                    value.result_id,
                    None if value.snapshot is None else value.snapshot.snapshot_id,
                    value.event.revision_id,
                    codec.dumps(value),
                ),
            )
            return True

    def propagation(self, result_id: str) -> PropagationResult | None:
        from eie.graph.validation import validate_propagation

        rows = self.store._rows("SELECT * FROM graph_propagation WHERE id=?", (result_id,))
        if not rows:
            return None
        row = rows[0]
        value = typed(row[-1], PropagationResult)
        if (
            row[:-1]
            != (
                value.result_id,
                None if value.snapshot is None else value.snapshot.snapshot_id,
                value.event.revision_id,
            )
            or not validate_propagation(value, self).valid
            or value.snapshot is not None
            and self.snapshot(value.snapshot.snapshot_id) != value.snapshot
        ):
            raise PersistenceError("propagation indexes/cutoff/recalculation disagree")
        return value
