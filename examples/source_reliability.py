"""Deterministic offline latency, two-time audit, source diagnostics and restart."""

from pathlib import Path
from tempfile import TemporaryDirectory

from eie.evidence.schema import ClaimStatus
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.models import (
    CoverageAssessment,
    CoverageSlot,
    CoverageUniverse,
    ReliabilityQuery,
    SourceReliabilityPolicy,
)
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import CoverageState
from eie.reliability.validation import validate
from examples.reliability_fixtures import DATASET, ISSUER, SECOND, ingest


def run(path: Path) -> dict[str, object]:
    policy = SourceReliabilityPolicy(minimum_count=3)
    q1 = ReliabilityQuery(DATASET.dataset_id, "A", "financial", 15 * SECOND, policy)
    with SQLiteEventStore(path) as store:
        first, a = ingest(store, "A", 10 * SECOND, publication=6 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        universe = CoverageUniverse(
            DATASET.dataset_id,
            "A",
            "financial",
            ISSUER,
            0,
            50 * SECOND,
            0,
            "synthetic-calendar-v1",
            "a" * 64,
            tuple(CoverageSlot(str(i), 5 * SECOND, "EARNINGS.RELEASE", ISSUER) for i in range(4)),
        )
        repo.append_universe(universe)
        original = engine.source_reliability(q1)
        audit = engine.audit(DATASET.dataset_id, a.subject_id, 15 * SECOND, 0)
        saved, audit_saved = dumps(original), dumps(audit)
        ingest(store, "B", 20 * SECOND, publication=12 * SECOND)
        _, correction = ingest(
            store,
            "A",
            30 * SECOND,
            publication=25 * SECOND,
            prior=first,
            value=101,
            status=ClaimStatus.CORRECTED,
        )
        for key, state, row in (
            ("0", CoverageState.OBSERVED, correction.artifact_id),
            ("1", CoverageState.KNOWN_EMPTY, None),
            ("2", CoverageState.MISSING, None),
        ):
            repo.append_assessment(
                CoverageAssessment(
                    universe.artifact_id,
                    key,
                    35 * SECOND,
                    state,
                    "synthetic-acquisition-review",
                    "b" * 64,
                    row,
                )
            )
        q2 = ReliabilityQuery(
            DATASET.dataset_id,
            "A",
            "financial",
            40 * SECOND,
            policy,
            coverage_universe_id=universe.artifact_id,
        )
        current = engine.source_reliability(q2)
        other = engine.source_reliability(
            ReliabilityQuery(DATASET.dataset_id, "B", "financial", 40 * SECOND, policy)
        )
        latency = engine.latency(a.artifact_id, 40 * SECOND)
        coverage = engine.coverage(universe.artifact_id, 40 * SECOND)
        for value in (original, current, other, audit, latency, coverage):
            validate(repo, value, deep=True)
        assert dumps(engine.source_reliability(q1)) == saved
        assert dumps(engine.audit(DATASET.dataset_id, a.subject_id, 15 * SECOND, 0)) == audit_saved
        result = {
            "source_class_A": current.source_classes,
            "source_class_B": other.source_classes,
            "A_sample_count": current.sample_count,
            "A_error_corrections": next(
                m.count for m in current.behavior if m.name == "ERROR_CORRECTION"
            ),
            "B_error_corrections": next(
                m.count for m in other.behavior if m.name == "ERROR_CORRECTION"
            ),
            "A_publication_to_observed_ns": latency.components[3].delta,
            "coverage": {
                "expected": coverage.expected_count,
                "observed": coverage.observed_count,
                "known_empty": coverage.known_empty_count,
                "missing": coverage.missing_count,
                "unknown": coverage.unknown_count,
                "ratio": coverage.ratio,
            },
            "historical_bytes_unchanged": True,
            "master_score": "not defined",
        }
        current_saved = dumps(current)
    with SQLiteEventStore(path) as store:
        engine = ReliabilityQueries(ReliabilityRepository(store))
        assert dumps(engine.source_reliability(q1)) == saved
        assert dumps(engine.source_reliability(q2)) == current_saved
        validate(engine.repository, original, deep=True)
        result["restart_bytes_identical"] = True
    return result


if __name__ == "__main__":
    import json

    with TemporaryDirectory() as directory:
        print(json.dumps(run(Path(directory) / "reliability.sqlite"), indent=2))
