"""Separate descriptive stability/completeness rates; none are source truth scores."""

from eie.reliability.models import RateMetric, SourceHistoryRow, SourceReliabilityPolicy


def rate(name: str, count: int, total: int) -> RateMetric:
    return RateMetric(name, count, total, count / total if total else None)


def calculate(
    rows: tuple[SourceHistoryRow, ...],
    predecessors: tuple[SourceHistoryRow, ...],
    policy: SourceReliabilityPolicy,
) -> tuple[RateMetric, ...]:
    prior = {r.source_revision_id: r for r in predecessors}
    pairs = [(r, prior[r.previous_source_id]) for r in rows if r.previous_source_id in prior]
    return (
        rate("SELF_REVISION", sum(r.number > 1 for r in rows), len(rows)),
        rate(
            "SAME_FIELD_VALUE_STABILITY",
            sum(
                a.value_fingerprint == b.value_fingerprint and a.subject_id == b.subject_id
                for a, b in pairs
            ),
            len(pairs),
        ),
        rate(
            "CLOCK_SCHEMA_COMPLETENESS",
            sum(
                all(
                    (clock := r.clocks.get(c)) is not None and clock.precision.value != "UNKNOWN"
                    for c in policy.required_clocks
                )
                for r in rows
            ),
            len(rows),
        ),
        rate(
            "PUBLICATION_TIMESTAMP_STABILITY",
            sum(
                a.clocks.publication == b.clocks.publication
                for a, b in pairs
                if a.clocks.publication is not None and b.clocks.publication is not None
            ),
            sum(
                a.clocks.publication is not None and b.clocks.publication is not None
                for a, b in pairs
            ),
        ),
        rate(
            "IDENTIFIER_STABILITY", sum(a.subject_id == b.subject_id for a, b in pairs), len(pairs)
        ),
    )
