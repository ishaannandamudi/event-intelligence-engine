"""Revision timelines preserve both actual local clocks and owner knowledge semantics."""

from eie.reliability.clocks import bounds
from eie.reliability.models import AuditEntry, BitemporalAuditResult, SourceHistoryRow
from eie.reliability.schema import ReliabilityQuality as Q


def calculate(
    dataset: str, subject: str, cutoff: int, valid_at: int, rows: tuple[SourceHistoryRow, ...]
) -> BitemporalAuditResult:
    visible = tuple(
        sorted(
            (r for r in rows if r.known_ns <= cutoff and r.metric_available_ns <= cutoff),
            key=lambda r: (r.known_ns, r.thread_id, r.number, r.artifact_id),
        )
    )
    latest: dict[str, SourceHistoryRow] = {}
    unknown: set[str] = set()
    quality: set[Q] = set()
    for row in visible:
        if row.reconstructed:
            quality.add(Q.RECONSTRUCTION_LIMITATION)
        interval = bounds(row.clocks.valid, row.clocks.coarse_timezone)
        if interval is None:
            unknown.add(row.artifact_id)
            quality.add(Q.UNKNOWN_VALIDITY)
            continue
        if interval[0] != interval[1]:
            # A supplied date describes the entire UTC date, not its midnight.
            applicable = interval[0] <= valid_at <= interval[1]
        else:
            applicable = valid_at >= interval[0] and (
                row.valid_to_ns is None or valid_at < row.valid_to_ns
            )
        if applicable and (
            row.thread_id not in latest or row.number > latest[row.thread_id].number
        ):
            latest[row.thread_id] = row
        if row.reconstructed:
            quality.add(Q.RECONSTRUCTION_LIMITATION)
    for row in visible:
        if (
            row.artifact_id in unknown
            and row.thread_id in latest
            and (row.number > latest[row.thread_id].number)
        ):
            del latest[row.thread_id]
    entries = tuple(
        AuditEntry(
            r.artifact_id,
            r.source_revision_id,
            r.thread_id,
            r.number,
            r.known_ns,
            r.metric_available_ns,
            r.clocks.valid,
            r.valid_to_ns,
            r.behavior,
            r.clocks.observed,
        )
        for r in visible
    )
    return BitemporalAuditResult(
        dataset,
        subject,
        cutoff,
        valid_at,
        entries,
        tuple(sorted(r.artifact_id for r in latest.values())),
        tuple(sorted(unknown)),
        frozenset(quality),
    )
