"""Atomic collection, calculation, persistence and cutoff sealing."""

from collections.abc import Iterable
from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.interactions.collection import collect
from eie.interactions.collisions import collision
from eie.interactions.contamination import contamination
from eie.interactions.models import (
    Artifact,
    EventCollision,
    EventSequence,
    EventStateTransition,
    InteractionContext,
    InteractionInputManifest,
    InteractionPolicy,
    InteractionQuery,
    PathReference,
    PropagationChain,
    PropagationChronology,
)
from eie.interactions.propagation import chronology
from eie.interactions.repository import InteractionRepository
from eie.interactions.schema import CollisionType, OrderingBasis
from eie.interactions.sequences import sequence
from eie.interactions.transitions import transitions
from eie.persistence import codec


@dataclass(frozen=True, slots=True)
class InteractionArtifacts:
    context: InteractionContext
    collisions: tuple[EventCollision, ...]
    sequences: tuple[EventSequence, ...]
    transitions: tuple[EventStateTransition, ...]
    chronology: tuple[PropagationChronology, ...]
    chains: tuple[PropagationChain, ...]
    paths: tuple[PathReference, ...]

    @property
    def children(self) -> tuple[Artifact, ...]:
        return (
            *self.collisions,
            *self.sequences,
            *self.transitions,
            *self.chronology,
            *self.chains,
            *self.paths,
        )


def calculate(
    repo: InteractionRepository, manifest: InteractionInputManifest, policy: InteractionPolicy
) -> InteractionArtifacts:
    q = manifest.query
    target = repo.event(manifest.target_row_id)
    candidate_rows = tuple(repo.event(i) for i in manifest.collision_row_ids)
    if (target.event_id, target.dataset_id) != (q.target_event_id, q.dataset_id) or (
        target.known_ns > q.known_as_of_ns or policy.artifact_id != q.policy_id
    ):
        raise ValidationError("interaction target/policy/cutoff disagreement")
    if len(candidate_rows) > policy.max_pairs + 1 or len(manifest.paths) > policy.max_paths:
        raise ValidationError("frozen interaction input bounds exceeded")
    identities = {e: g[0] for g in manifest.identity_groups for e in g}
    candidate_keys = [identities.get(r.event_id, r.event_id) for r in candidate_rows]
    if len(set(candidate_keys)) != len(candidate_keys):
        raise ValidationError("same EIE-11 event identity cannot collide with itself")
    valid = target.occurrence.timestamp_ns
    if valid is None:
        valid = target.known_ns
    for p in manifest.paths:
        if (
            p.graph_id != q.graph_id
            or p.known_ns > q.known_as_of_ns
            or p.valid_ns != valid
            or (
                p.node_ids[0] not in target.scope.identifiers
                or p.hop_count > policy.max_hops
                or any(k not in policy.relationship_types for k in p.relationship_types)
            )
        ):
            raise ValidationError("frozen graph path timing/roots/policy disagreement")
    all_ids = {
        *manifest.collision_row_ids,
        *manifest.sequence_row_ids,
        *manifest.transition_row_ids,
        *manifest.propagation_row_ids,
    }
    for identity in all_ids:
        row = repo.event(identity)
        if row.dataset_id != q.dataset_id or row.known_ns > q.known_as_of_ns:
            raise ValidationError("future/mismatched frozen interaction member")
    root_identity = identities.get(target.event_id, target.event_id)
    if any(
        identities.get(repo.event(i).event_id, repo.event(i).event_id) == root_identity
        for i in manifest.propagation_row_ids
    ):
        raise ValidationError("same root event alias cannot propagate to itself")
    collisions = []
    for other in candidate_rows:
        if other.artifact_id == target.artifact_id:
            continue
        paths = tuple(p for p in manifest.paths if p.node_ids[-1] in other.scope.identifiers)
        c = collision(target, other, q.known_as_of_ns, policy.window, paths)
        if c is not None and (not q.collision_types or set(c.categories) & set(q.collision_types)):
            collisions.append(c)
    collisions.sort(key=lambda c: c.artifact_id)
    sequence_rows = tuple(repo.event(i) for i in manifest.sequence_row_ids)
    if len(sequence_rows) > policy.max_sequence_events:
        raise ValidationError("interaction sequence member bound exceeded")
    seqs = tuple(
        sequence(
            sequence_rows,
            q.dataset_id,
            target.event_id,
            0,
            q.known_as_of_ns,
            q.known_as_of_ns,
            basis,
            q.event_resolution_id,
        )
        for basis in (OrderingBasis.EXACT_EVENT_TIME, OrderingBasis.KNOWLEDGE_TIME)
    )
    changes = transitions(tuple(repo.event(i) for i in manifest.transition_row_ids))
    entries, chains = chronology(
        target,
        tuple(repo.event(i) for i in manifest.propagation_row_ids),
        manifest.paths,
        policy,
        q.known_as_of_ns,
    )
    members = tuple(
        sorted(
            {
                manifest.target_row_id,
                *manifest.collision_row_ids,
                *manifest.sequence_row_ids,
                *manifest.transition_row_ids,
                *manifest.propagation_row_ids,
            }
        )
    )
    quality = set(target.quality)
    qualified: Iterable[
        EventCollision | EventSequence | PropagationChronology | PropagationChain
    ] = (*collisions, *seqs, *entries, *chains)
    for obj in qualified:
        quality.update(obj.quality)
    counts = tuple(
        (kind, n)
        for kind in sorted(CollisionType)
        if (n := sum(kind in c.categories for c in collisions))
    )
    context = InteractionContext(
        q,
        target.artifact_id,
        members,
        tuple(c.artifact_id for c in collisions),
        tuple(sorted(s.artifact_id for s in seqs)),
        tuple(t.artifact_id for t in changes),
        tuple(e.artifact_id for e in entries),
        tuple(c.artifact_id for c in chains),
        tuple(p.artifact_id for p in manifest.paths),
        contamination(tuple(collisions)),
        counts,
        frozenset(quality),
        len(candidate_rows),
    )
    result = InteractionArtifacts(
        context, tuple(collisions), seqs, changes, entries, chains, manifest.paths
    )
    if (
        sum(len(codec.dumps(v)) for v in (context, manifest, *result.children))
        > policy.max_output_bytes
    ):
        raise ValidationError("interaction snapshot serialized output bound exceeded")
    return result


class InteractionEngine:
    def __init__(self, repository: InteractionRepository) -> None:
        self.repository = repository

    def interaction_context(self, query: InteractionQuery) -> InteractionContext:
        repo = self.repository
        with repo.store.transaction():
            prior = repo.store._rows(
                "SELECT id FROM interaction_contexts WHERE request=?", (query.artifact_id,)
            )
            if prior:
                return repo.context(str(prior[0][0]))
            policy = repo.policy(query.policy_id)
            manifest = collect(repo, query, policy)
            normalized = repo.store._rows(
                "SELECT id FROM interaction_contexts WHERE request=?",
                (manifest.query.artifact_id,),
            )
            if normalized:
                return repo.context(str(normalized[0][0]))
            result = calculate(repo, manifest, policy)
            same = repo.store._rows(
                "SELECT id FROM interaction_contexts WHERE id=?", (result.context.artifact_id,)
            )
            if same:
                return repo.context(result.context.artifact_id)
            for value in result.children:
                repo.put(value)
            for c in result.collisions:
                repo.store._connection.executemany(
                    "INSERT OR IGNORE INTO interaction_pairs VALUES (?,?,?,?)",
                    ((c.artifact_id, c.left_row_id, c.right_row_id, k.value) for k in c.categories),
                )
            for entry in result.chronology:
                repo.store._connection.execute(
                    "INSERT OR IGNORE INTO interaction_chronology VALUES (?,?,?,?)",
                    (entry.artifact_id, entry.root_row_id, entry.later_row_id, entry.path_id),
                )
            repo.save_context(result.context, query.artifact_id, manifest)
            return result.context

    def run(self, queries: tuple[InteractionQuery, ...]) -> str:
        repo = self.repository
        if not queries:
            raise ValidationError("interaction run requires target queries")
        policy = repo.policy(queries[0].policy_id)
        if len(queries) > policy.max_run_members:
            raise ValidationError("interaction run member bound exceeded")
        with repo.store.transaction():
            return repo.append_run(tuple(self.interaction_context(q) for q in queries)).artifact_id
