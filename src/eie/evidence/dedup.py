"""Bounded blocks and complete-link groups avoid unsafe transitive merges."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.evidence.claims import ordered
from eie.evidence.matching import (
    DEFAULT_DEDUPE_POLICY,
    DedupePolicy,
    DuplicateCandidate,
    EventDescriptor,
    blocks,
    match_events,
)
from eie.evidence.models import DEFAULT_EVIDENCE_LIMITS, EvidenceLimits
from eie.evidence.schema import DedupeDecision
from eie.studies.checks import contract


@dataclass(frozen=True, slots=True)
class EventResolutionGroup:
    member_event_ids: tuple[str, ...]
    descriptor_ids: tuple[str, ...]
    representative_event_id: str
    policy_fingerprint: str

    def __post_init__(self) -> None:
        contract(self)
        ordered(self.member_event_ids, 1000, empty=False)
        ordered(self.descriptor_ids, 1000, empty=False)
        if self.representative_event_id not in self.member_event_ids:
            raise ValidationError("group representative must be a retained member")

    @property
    def group_id(self) -> str:
        return "egroup_" + fingerprint(
            "eie:event-group:v1", (self.member_event_ids, self.policy_fingerprint)
        )


@dataclass(frozen=True, slots=True)
class EventResolutionRun:
    dataset_id: str
    known_as_of_ns: int
    descriptor_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    groups: tuple[EventResolutionGroup, ...]
    truncated_event_ids: tuple[str, ...]
    policy: DedupePolicy
    limits: EvidenceLimits

    def __post_init__(self) -> None:
        contract(self)
        for values in (self.descriptor_ids, self.candidate_ids, self.truncated_event_ids):
            ordered(values, 100000)
        if tuple(g.group_id for g in self.groups) != tuple(
            sorted({g.group_id for g in self.groups})
        ):
            raise ValidationError("ordered distinct event groups required")
        if sorted(d for g in self.groups for d in g.descriptor_ids) != list(
            self.descriptor_ids
        ) or len({e for g in self.groups for e in g.member_event_ids}) != sum(
            len(g.member_event_ids) for g in self.groups
        ):
            raise ValidationError("event resolution must partition exact active inputs")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:event-resolution-run:v1", self)

    @property
    def run_id(self) -> str:
        return "erun_" + self.result_fingerprint


def resolve_events(
    rows: tuple[EventDescriptor, ...],
    dataset_id: str,
    known_as_of_ns: int,
    policy: DedupePolicy = DEFAULT_DEDUPE_POLICY,
    limits: EvidenceLimits = DEFAULT_EVIDENCE_LIMITS,
) -> tuple[EventResolutionRun, tuple[DuplicateCandidate, ...]]:
    require_ns(known_as_of_ns)
    if len(rows) > limits.query_size or any(r.dataset_id != dataset_id for r in rows):
        raise ValidationError("bounded single-dataset event resolution required")
    revisions: dict[tuple[str, int], EventDescriptor] = {}
    for row in rows:
        if row.known_from_ns <= known_as_of_ns:
            revision_key = (row.thread_id, row.number)
            prior = revisions.get(revision_key)
            if prior is not None and prior != row:
                raise ValidationError("conflicting provider event revision inputs")
            revisions[revision_key] = row
    latest: dict[str, EventDescriptor] = {}
    for row in revisions.values():
        if row.thread_id not in latest or row.number > latest[row.thread_id].number:
            latest[row.thread_id] = row
    active = tuple(sorted(latest.values(), key=lambda r: r.descriptor_id))
    index: dict[str, list[EventDescriptor]] = {}
    for row in active:
        for key in blocks(row):
            index.setdefault(key, []).append(row)
    candidates: dict[tuple[str, str], DuplicateCandidate] = {}
    truncated: set[str] = set()
    for row in active:
        neighbors: dict[str, EventDescriptor] = {}
        for key in blocks(row):
            for other in index[key][: limits.candidates_per_event + 2]:
                if other.descriptor_id != row.descriptor_id:
                    neighbors[other.descriptor_id] = other
            if len(index[key]) > limits.candidates_per_event + 1:
                truncated.add(row.event_id)
        ordered_neighbors = sorted(neighbors.values(), key=lambda r: r.descriptor_id)
        if len(ordered_neighbors) > limits.candidates_per_event:
            truncated.add(row.event_id)
        for other in ordered_neighbors[: limits.candidates_per_event]:
            pair = (
                min(row.descriptor_id, other.descriptor_id),
                max(row.descriptor_id, other.descriptor_id),
            )
            if pair not in candidates:
                candidates[pair] = match_events(row, other, policy)
    # A pair nominated by either side must still respect the per-event result bound.
    kept: dict[tuple[str, str], DuplicateCandidate] = {}
    counts: dict[str, int] = {}
    for pair, candidate in sorted(candidates.items()):
        if any(counts.get(d, 0) >= limits.candidates_per_event for d in pair):
            truncated.update((candidate.left_event_id, candidate.right_event_id))
            continue
        kept[pair] = candidate
        for d in pair:
            counts[d] = counts.get(d, 0) + 1
    by_id = {r.descriptor_id: r for r in active}
    groups: list[set[str]] = [{r.descriptor_id} for r in active]
    # Same canonical EIE identity is one retained event; conflicting source descriptors
    # still participate in candidates and prevent merging that event with another event.
    for event_id in sorted({r.event_id for r in active}):
        same = [g for g in groups if any(by_id[d].event_id == event_id for d in g)]
        if len(same) > 1:
            union = set().union(*same)
            if len(union) > limits.group_members:
                raise ValidationError("canonical event descriptor membership bound exceeded")
            if any(
                match_events(by_id[x], by_id[y], policy).decision is DedupeDecision.KEEP_SEPARATE
                for x in union
                for y in union
                if x < y
            ):
                raise ValidationError(
                    "conflicting hard identities require distinct canonical event IDs"
                )
            groups = [g for g in groups if g not in same] + [union]
    for candidate in kept.values():
        if candidate.decision is not DedupeDecision.MERGE:
            continue
        a = next(g for g in groups if candidate.left_id in g)
        b = next(g for g in groups if candidate.right_id in g)
        if a is b or len(a | b) > limits.group_members:
            if a is not b:
                truncated.update(by_id[d].event_id for d in a | b)
            continue
        if any(by_id[d].event_id in truncated for d in a | b):
            continue
        # Every cross-pair needs a retained MERGE; no inference across missing pairs.
        if all(
            (min(x, y), max(x, y)) in kept
            and kept[(min(x, y), max(x, y))].decision is DedupeDecision.MERGE
            for x in a
            for y in b
        ):
            groups.remove(a)
            groups.remove(b)
            groups.append(a | b)
    result_groups = tuple(
        sorted(
            (
                EventResolutionGroup(
                    tuple(sorted({by_id[d].event_id for d in g})),
                    tuple(sorted(g)),
                    min(by_id[d].event_id for d in g),
                    policy.fingerprint,
                )
                for g in groups
            ),
            key=lambda g: g.group_id,
        )
    )
    result_candidates = tuple(sorted(kept.values(), key=lambda c: c.candidate_id))
    return EventResolutionRun(
        dataset_id,
        known_as_of_ns,
        tuple(r.descriptor_id for r in active),
        tuple(c.candidate_id for c in result_candidates),
        result_groups,
        tuple(sorted(truncated)),
        policy,
        limits,
    ), result_candidates
