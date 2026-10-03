"""Exact compact recalculation; deep validation additionally verifies retained owners."""

from eie.core.errors import PersistenceError
from eie.reliability import bitemporal, coverage, latency, reliability
from eie.reliability.models import (
    BitemporalAuditResult,
    CoverageUniverse,
    InformationLatency,
    SourceCoverageResult,
    SourceReliabilityResult,
)
from eie.reliability.repository import ReliabilityRepository


def validate(
    repo: ReliabilityRepository,
    value: SourceReliabilityResult
    | BitemporalAuditResult
    | InformationLatency
    | SourceCoverageResult,
    *,
    deep: bool = False,
) -> None:
    rebuilt: (
        SourceReliabilityResult | BitemporalAuditResult | InformationLatency | SourceCoverageResult
    )
    with repo.store.transaction():
        if isinstance(value, SourceReliabilityResult):
            rows = tuple(repo.row(i, deep=deep) for i in value.input_row_ids)
            peers = tuple(repo.row(i, deep=deep) for i in value.peer_row_ids)
            cover = (
                None
                if value.coverage_id is None
                else repo.artifact(value.coverage_id, SourceCoverageResult)
            )
            rebuilt = reliability.calculate(
                value.query,
                rows,
                peers,
                value.coverage_id,
                frozenset() if cover is None else cover.quality,
            )
            if value.coverage_id is not None:
                validate(repo, repo.artifact(value.coverage_id, SourceCoverageResult), deep=deep)
            if deep and repo.collect(value.query) != (rows, peers):
                raise PersistenceError("reliability pinned sample selection disagreement")
        elif isinstance(value, InformationLatency):
            rebuilt = latency.calculate(repo.row(value.row_id, deep=deep), value.known_as_of_ns)
        elif isinstance(value, BitemporalAuditResult):
            rows = tuple(repo.row(e.row_id, deep=deep) for e in value.entries)
            rebuilt = bitemporal.calculate(
                value.dataset_id, value.subject_id, value.known_as_of_ns, value.valid_at_ns, rows
            )
            if deep:
                ids = repo.ids(
                    "SELECT e.id FROM reliability_rows e WHERE dataset=? AND known<=? "
                    "AND available<=? AND "
                    "(subject=? OR thread=? OR source_revision=?) AND "
                    + repo.latest_frame(value.known_as_of_ns, inclusive=True)
                    + " ORDER BY e.id",
                    (
                        value.dataset_id,
                        value.known_as_of_ns,
                        value.known_as_of_ns,
                        value.subject_id,
                        value.subject_id,
                        value.subject_id,
                    ),
                    10000,
                )
                if set(ids) != {r.artifact_id for r in rows}:
                    raise PersistenceError("audit historical selection disagreement")
        else:
            universe = (
                None
                if value.universe_id is None
                else repo.artifact(value.universe_id, CoverageUniverse)
            )
            assessments = (
                ()
                if value.universe_id is None
                else repo.assessments(value.universe_id, value.known_as_of_ns, 100000)
            )
            if deep:
                for a in assessments:
                    if a.row_id is not None:
                        repo.row(a.row_id, deep=True)
            rebuilt = coverage.calculate(universe, assessments, value.known_as_of_ns)
        if rebuilt != value:
            raise PersistenceError("source diagnostic recalculation disagreement")
