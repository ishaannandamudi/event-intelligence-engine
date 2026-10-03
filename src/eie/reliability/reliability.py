"""Independent empirical components using exact retained EIE-11 claim/family identity."""

from eie.core.errors import ValidationError
from eie.evidence.claims import TimePrecision
from eie.evidence.resolution import compatible
from eie.reliability.aggregation import distributions, select
from eie.reliability.consistency import calculate as consistency
from eie.reliability.consistency import rate
from eie.reliability.models import ReliabilityQuery, SourceHistoryRow, SourceReliabilityResult
from eie.reliability.schema import ReliabilityQuality as Q
from eie.reliability.schema import RevisionBehavior, SubjectType


def calculate(
    query: ReliabilityQuery,
    rows: tuple[SourceHistoryRow, ...],
    peers: tuple[SourceHistoryRow, ...],
    coverage_id: str | None = None,
    coverage_quality: frozenset[Q] = frozenset(),
) -> SourceReliabilityResult:
    matching = tuple(
        r
        for r in rows
        if (
            r.dataset_id == query.dataset_id
            and r.source.name == query.provider
            and r.resource_id == query.resource_id
            and r.kind is query.kind
            and (query.event_type is None or r.event_type == query.event_type)
            and (query.field is None or r.field == query.field)
            and (query.scope_id is None or r.scope_id == query.scope_id)
        )
    )
    sample = select(query, matching)
    if len({r.field for r in sample}) > query.policy.max_fields:
        raise ValidationError("field bound exceeded")
    threads: dict[str, int] = {}
    for row in rows:
        threads[row.thread_id] = threads.get(row.thread_id, 0) + 1
    if max(threads.values(), default=0) > query.policy.max_revisions or any(
        r.number > query.policy.max_revisions for r in rows
    ):
        raise ValidationError("revision bound exceeded")
    visible_peers = tuple(p for p in peers if p.metric_available_ns < query.known_as_of_ns)
    if len(visible_peers) > query.policy.max_observations:
        raise ValidationError("corroboration peer bound exceeded")
    current: dict[str, SourceHistoryRow] = {}
    for row in visible_peers:
        old = current.get(row.thread_id)
        if old is None or row.number > old.number:
            current[row.thread_id] = row
    by_subject: dict[str, list[SourceHistoryRow]] = {}
    for peer in current.values():
        by_subject.setdefault(peer.subject_id, []).append(peer)
    confirmed = conflicted = 0
    for row in sample:
        alternatives = [
            p
            for p in by_subject.get(row.subject_id, ())
            if p.kind is SubjectType.CLAIM
            and p.active
            and row.active
            and (p.content_time is None or p.content_time.precision is not TimePrecision.UNKNOWN)
            and (
                row.content_time is None or row.content_time.precision is not TimePrecision.UNKNOWN
            )
            and p.subject_id == row.subject_id
            and p.source_family != row.source_family
        ]

        def agrees(peer: SourceHistoryRow, row: SourceHistoryRow = row) -> bool:
            if row.content_time is not None and peer.content_time is not None:
                return compatible(row.content_time, peer.content_time)
            return peer.value_fingerprint == row.value_fingerprint

        confirmed += any(agrees(p) for p in alternatives)
        conflicted += any(not agrees(p) for p in alternatives)
    quality: set[Q] = set(coverage_quality)
    if len(sample) < query.policy.minimum_count:
        quality.add(Q.LOW_SAMPLE)
    if any(r.reconstructed for r in sample):
        quality.add(Q.RECONSTRUCTION_LIMITATION)
    if any(r.source_family == r.source.name for r in sample):
        quality.add(Q.SOURCE_FAMILY_AMBIGUITY)
    if conflicted:
        quality.add(Q.CONFLICT_GROUND_TRUTH_UNKNOWN)
    if coverage_id is None:
        quality.add(Q.INCOMPLETE_UNIVERSE)
    return SourceReliabilityResult(
        query,
        tuple(sorted(r.artifact_id for r in rows)),
        tuple(r.artifact_id for r in sample),
        tuple(sorted(p.artifact_id for p in visible_peers)),
        len(sample),
        tuple(sorted({r.source.source_class.value for r in sample})),
        tuple(
            rate(b.value, sum(r.behavior is b for r in sample), len(sample))
            for b in RevisionBehavior
        ),
        consistency(sample, rows, query.policy),
        rate("INDEPENDENT_EXACT_CONFIRMATION", confirmed, len(sample)),
        rate("INDEPENDENT_EXACT_DISAGREEMENT", conflicted, len(sample)),
        distributions(sample, query),
        coverage_id,
        frozenset(quality),
    )
