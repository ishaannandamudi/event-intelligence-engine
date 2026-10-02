"""Bounded explicit-cutoff claim, event and source audit APIs."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.evidence.claims import Claim, ProviderClaimObservation
from eie.evidence.dedup import EventResolutionGroup, EventResolutionRun, resolve_events
from eie.evidence.history import ClaimKnowledgeInterval, knowledge_intervals
from eie.evidence.matching import DEFAULT_DEDUPE_POLICY, DedupePolicy, DuplicateCandidate
from eie.evidence.models import (
    DEFAULT_EVIDENCE_LIMITS,
    DEFAULT_RESOLUTION_POLICY,
    CanonicalClaimResolution,
    EvidenceLimits,
    ResolutionPolicy,
)
from eie.evidence.references import EvidenceReference
from eie.evidence.repository import EvidenceRepository
from eie.evidence.resolution import resolve_claim
from eie.evidence.schema import EvidenceRelation, Predicate
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class EvidenceTraversal:
    assertion_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    truncated: bool

    def __post_init__(self) -> None:
        contract(self)


class EvidenceQueries:
    def __init__(
        self,
        repository: EvidenceRepository,
        dataset_id: str,
        limits: EvidenceLimits = DEFAULT_EVIDENCE_LIMITS,
    ) -> None:
        self.repository, self.dataset_id, self.limits = repository, dataset_id, limits

    def claim(self, claim_id: str) -> Claim:
        return self.repository.claim(claim_id)

    def claims(
        self, known_as_of_ns: int, *, subject: str | None = None, predicate: Predicate | None = None
    ) -> tuple[Claim, ...]:
        require_ns(known_as_of_ns)
        clauses, args = ["a.dataset=?", "a.known<=?"], [self.dataset_id, known_as_of_ns]
        for column, value in (
            ("c.subject", subject),
            ("c.predicate", predicate.value if predicate is not None else None),
        ):
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        ids = self.repository._ids(
            "SELECT DISTINCT c.id FROM ce_claims c JOIN ce_assertions a ON c.id=a.claim WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.id",
            tuple(args),
            self.limits.query_size,
        )
        return tuple(self.claim(i) for i in ids)

    def claim_history(
        self, claim_id: str, known_as_of_ns: int
    ) -> tuple[ClaimKnowledgeInterval, ...]:
        return knowledge_intervals(
            self.repository.observations(
                self.dataset_id, known_as_of_ns, claim_id=claim_id, limit=self.limits.query_size
            ),
            known_as_of_ns,
        )

    def resolution(
        self,
        claim_id: str,
        known_as_of_ns: int,
        valid_at_ns: int | None = None,
        policy: ResolutionPolicy = DEFAULT_RESOLUTION_POLICY,
    ) -> CanonicalClaimResolution:
        with self.repository.store.transaction():
            rows = self.repository.observations(
                self.dataset_id, known_as_of_ns, claim_id=claim_id, limit=self.limits.query_size
            )
            links = self.repository.relationships(
                self.dataset_id,
                known_as_of_ns,
                tuple(r.observation_id for r in rows),
                self.limits.query_size,
            )
            return resolve_claim(
                self.claim(claim_id),
                self.dataset_id,
                rows,
                links,
                known_as_of_ns,
                valid_at_ns,
                policy,
            )

    def claims_as_of(
        self,
        known_as_of_ns: int,
        valid_at_ns: int | None = None,
        policy: ResolutionPolicy = DEFAULT_RESOLUTION_POLICY,
    ) -> tuple[CanonicalClaimResolution, ...]:
        with self.repository.store.transaction():
            return tuple(
                self.resolution(c.claim_id, known_as_of_ns, valid_at_ns, policy)
                for c in self.claims(known_as_of_ns)
            )

    def evidence_for_claim(
        self, claim_id: str, known_as_of_ns: int
    ) -> tuple[EvidenceReference, ...]:
        result = self.resolution(claim_id, known_as_of_ns)
        return tuple(self.repository.reference(i) for i in result.evidence_ids)

    def supporting_claims(
        self, claim_id: str, known_as_of_ns: int
    ) -> tuple[ProviderClaimObservation, ...]:
        from eie.evidence.resolution import compatible

        result = self.resolution(claim_id, known_as_of_ns)
        return tuple(
            r
            for i in result.active_ids
            if result.value is not None
            and compatible((r := self.repository.assertion(i)).value, result.value)
        )

    def contradictions(
        self, claim_id: str, known_as_of_ns: int
    ) -> tuple[ProviderClaimObservation, ...]:
        result = self.resolution(claim_id, known_as_of_ns)
        ids = sorted({i for c in result.conflicts for i in (c.left_id, c.right_id)})
        return tuple(self.repository.assertion(i) for i in ids)

    def source_item(
        self, raw_observation_id: str, known_as_of_ns: int
    ) -> tuple[ProviderClaimObservation, ...]:
        return self.repository.observations(
            self.dataset_id, known_as_of_ns, raw_id=raw_observation_id, limit=self.limits.query_size
        )

    def event_claims(
        self, event_id: str, known_as_of_ns: int
    ) -> tuple[ProviderClaimObservation, ...]:
        return self.repository.observations(
            self.dataset_id, known_as_of_ns, event_id=event_id, limit=self.limits.query_size
        )

    def event_sources(self, event_id: str, known_as_of_ns: int) -> tuple[str, ...]:
        group = self.event_group(event_id, known_as_of_ns)
        events = (event_id,) if group is None else group.member_event_ids
        descriptors = self.repository.descriptors(
            self.dataset_id, known_as_of_ns, self.limits.query_size
        )
        return tuple(
            sorted(
                {r.raw_observation_id for r in descriptors if r.event_id in events}
                | {
                    r.raw_observation_id
                    for e in events
                    for r in self.event_claims(e, known_as_of_ns)
                }
            )
        )

    def event_field_resolutions(
        self,
        event_id: str,
        known_as_of_ns: int,
        valid_at_ns: int | None = None,
        policy: ResolutionPolicy = DEFAULT_RESOLUTION_POLICY,
    ) -> tuple[CanonicalClaimResolution, ...]:
        with self.repository.store.transaction():
            group = self.event_group(event_id, known_as_of_ns)
            members = (event_id,) if group is None else group.member_event_ids
            ids = sorted(
                {
                    r.claim_id
                    for member in members
                    for r in self.event_claims(member, known_as_of_ns)
                }
            )
            if len(ids) > self.limits.query_size:
                raise ValidationError("event field result bound exceeded")
            return tuple(self.resolution(i, known_as_of_ns, valid_at_ns, policy) for i in ids)

    def event_resolution(
        self, known_as_of_ns: int, policy: DedupePolicy = DEFAULT_DEDUPE_POLICY
    ) -> EventResolutionRun:
        with self.repository.store.transaction():
            return resolve_events(
                self.repository.descriptors(
                    self.dataset_id, known_as_of_ns, self.limits.query_size
                ),
                self.dataset_id,
                known_as_of_ns,
                policy,
                self.limits,
            )[0]

    def duplicate_candidates(
        self, known_as_of_ns: int, policy: DedupePolicy = DEFAULT_DEDUPE_POLICY
    ) -> tuple[DuplicateCandidate, ...]:
        with self.repository.store.transaction():
            return resolve_events(
                self.repository.descriptors(
                    self.dataset_id, known_as_of_ns, self.limits.query_size
                ),
                self.dataset_id,
                known_as_of_ns,
                policy,
                self.limits,
            )[1]

    def event_group(
        self, event_id: str, known_as_of_ns: int, policy: DedupePolicy = DEFAULT_DEDUPE_POLICY
    ) -> EventResolutionGroup | None:
        return next(
            (
                g
                for g in self.event_resolution(known_as_of_ns, policy).groups
                if event_id in g.member_event_ids
            ),
            None,
        )

    def resolved_event(self, event_id: str, known_as_of_ns: int) -> str | None:
        group = self.event_group(event_id, known_as_of_ns)
        return None if group is None else group.representative_event_id

    def resolution_as_of(self, known_as_of_ns: int) -> EventResolutionRun:
        return self.event_resolution(known_as_of_ns)

    def event_resolution_history(self, known_as_of_ns: int) -> tuple[EventResolutionRun, ...]:
        ids = self.repository._ids(
            "SELECT id FROM ce_event_runs WHERE dataset=? AND known<=? ORDER BY known,id",
            (self.dataset_id, known_as_of_ns),
            self.limits.query_size,
        )
        return tuple(self.repository.stored_event_run(i) for i in ids)

    def traverse(
        self,
        raw_observation_id: str,
        known_as_of_ns: int,
        *,
        depth: int = 2,
        max_results: int = 1000,
    ) -> EvidenceTraversal:
        if (
            isinstance(depth, bool)
            or not isinstance(depth, int)
            or not 0 <= depth <= self.limits.traversal_depth
            or isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= self.limits.query_size
        ):
            raise ValidationError("bounded evidence traversal required")
        with self.repository.store.transaction():
            rows = self.source_item(raw_observation_id, known_as_of_ns)
            seen = {r.observation_id for r in rows[:max_results]}
            frontier = set(seen)
            links: set[str] = set()
            truncated = len(rows) > max_results
            for _ in range(depth):
                next_frontier: set[str] = set()
                for relation in self.repository.relationships(
                    self.dataset_id, known_as_of_ns, tuple(sorted(frontier)), self.limits.query_size
                ):
                    if len(links) >= max_results and relation.relationship_id not in links:
                        truncated = True
                        continue
                    links.add(relation.relationship_id)
                    for identity in (relation.source_id, relation.target_id):
                        if identity not in seen:
                            if len(seen) >= max_results:
                                truncated = True
                            else:
                                seen.add(identity)
                                next_frontier.add(identity)
                frontier = next_frontier
            assertions = tuple(self.repository.assertion(i) for i in sorted(seen))
            evidence = sorted({e for r in assertions for e in r.evidence_ids})
            claims = sorted({r.claim_id for r in assertions})
            truncated |= len(evidence) > max_results or len(claims) > max_results
            return EvidenceTraversal(
                tuple(sorted(seen)),
                tuple(claims[:max_results]),
                tuple(evidence[:max_results]),
                tuple(sorted(links)),
                truncated,
            )

    def relationship_kind(
        self, claim_id: str, known_as_of_ns: int, relation: EvidenceRelation
    ) -> tuple[str, ...]:
        rows = self.repository.observations(
            self.dataset_id, known_as_of_ns, claim_id=claim_id, limit=self.limits.query_size
        )
        return tuple(
            r.relationship_id
            for r in self.repository.relationships(
                self.dataset_id,
                known_as_of_ns,
                tuple(r.observation_id for r in rows),
                self.limits.query_size,
            )
            if r.relation is relation
        )
