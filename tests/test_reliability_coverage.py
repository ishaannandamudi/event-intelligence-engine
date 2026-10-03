from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.coverage import calculate
from eie.reliability.models import CoverageAssessment, CoverageSlot, CoverageUniverse
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import CoverageState as S
from eie.reliability.validation import validate
from examples.reliability_fixtures import DATASET, ISSUER, SECOND, ingest


def universe(count=100):
    return CoverageUniverse(
        DATASET.dataset_id,
        "A",
        "financial",
        ISSUER,
        0,
        100 * SECOND,
        0,
        "synthetic-expected-release-manifest",
        fingerprint("eie:coverage-demo:v1", count),
        tuple(
            CoverageSlot(f"slot-{i:03}", 10 * SECOND, "EARNINGS.RELEASE", ISSUER)
            for i in range(count)
        ),
    )


def assessment(u, key, state, known=50 * SECOND, row=None):
    return CoverageAssessment(
        u.artifact_id, key, known, state, "synthetic-assessment", "a" * 64, row
    )


def test_explicit_100_release_denominator_95_observed_3_missing_2_unknown():
    with SQLiteEventStore(":memory:") as store:
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        rows = [ingest(store, "A", 20 * SECOND, key=f"item-{i}")[1] for i in range(95)]
        u = universe()
        repo.append_universe(u)
        for i, row in enumerate(rows):
            repo.append_assessment(assessment(u, f"slot-{i:03}", S.OBSERVED, row=row.artifact_id))
        for i in range(95, 98):
            repo.append_assessment(assessment(u, f"slot-{i:03}", S.MISSING))
        result = engine.coverage(u.artifact_id, 60 * SECOND)
        assert (
            result.expected_count,
            result.observed_count,
            result.missing_count,
            result.unknown_count,
        ) == (100, 95, 3, 2)
        assert result.ratio == 0.95
        validate(repo, result, deep=True)
        assert engine.coverage(None, 60 * SECOND).ratio is None


def test_known_empty_not_applicable_unknown_and_future_slots_are_distinct():
    u = universe(4)
    rows = tuple(
        assessment(u, f"slot-{i:03}", state)
        for i, state in enumerate((S.KNOWN_EMPTY, S.NOT_APPLICABLE, S.MISSING, S.UNKNOWN))
    )
    result = calculate(u, rows, 60 * SECOND)
    assert (
        result.known_empty_count,
        result.not_applicable_count,
        result.missing_count,
        result.unknown_count,
    ) == (1, 1, 1, 1)
    assert result.ratio == 0
    assert calculate(u, rows, 5 * SECOND).unknown_count == 4
    assert calculate(replace(u, slots=()), (), 60 * SECOND).ratio is None
    with pytest.raises(ValidationError):
        calculate(None, rows, 60 * SECOND)
    with pytest.raises(ValidationError):
        calculate(u, rows, 0)
    with pytest.raises(ValidationError):
        calculate(u, (replace(rows[0], slot_key="bad"),), 60 * SECOND)
    with pytest.raises(ValidationError):
        calculate(u, (rows[0], replace(rows[0], state=S.MISSING)), 60 * SECOND)


def test_assessment_provenance_attribution_and_future_changes():
    from eie.persistence.codec import dumps

    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 20 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        u = universe(1)
        repo.append_universe(u)
        with pytest.raises(ValidationError):
            repo.append_assessment(
                assessment(u, "slot-000", S.OBSERVED, known=19 * SECOND, row=row.artifact_id)
            )
        other = replace(u, provider="B")
        repo.append_universe(other)
        with pytest.raises(ValidationError):
            repo.append_assessment(assessment(other, "slot-000", S.OBSERVED, row=row.artifact_id))
        repo.append_assessment(assessment(u, "slot-000", S.UNKNOWN, known=30 * SECOND))
        initial = engine.coverage(u.artifact_id, 40 * SECOND)
        saved = dumps(initial)
        repo.append_assessment(assessment(u, "slot-000", S.KNOWN_EMPTY, known=50 * SECOND))
        assert dumps(engine.coverage(u.artifact_id, 40 * SECOND)) == saved
        assert engine.coverage(u.artifact_id, 60 * SECOND).known_empty_count == 1
        with pytest.raises(ValidationError):
            repo.append_assessment(assessment(u, "slot-000", S.MISSING, known=35 * SECOND))
