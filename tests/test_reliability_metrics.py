from dataclasses import replace

import pytest

from eie.evidence.schema import ClaimStatus
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.models import ProcessingTiming, ReliabilityQuery, SourceReliabilityPolicy
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import ReliabilityQuality as Q
from eie.reliability.schema import WindowKind
from eie.reliability.validation import validate
from examples.reliability_fixtures import DATASET, SECOND, ingest


def query(cutoff=100 * SECOND, **kwargs):
    return ReliabilityQuery(
        DATASET.dataset_id,
        "A",
        "financial",
        cutoff,
        kwargs.pop("policy", SourceReliabilityPolicy()),
        **kwargs,
    )


def metric(result, name):
    return next(m for m in result.behavior if m.name == name)


def test_explicit_correction_retraction_update_and_non_additive_metrics():
    with SQLiteEventStore(":memory:") as store:
        first, a = ingest(store, "A", 10 * SECOND, publication=6 * SECOND)
        update, b = ingest(
            store, "A", 20 * SECOND, prior=first, value=101, status=ClaimStatus.UPDATED
        )
        correction, c = ingest(
            store, "A", 30 * SECOND, prior=update, value=100, status=ClaimStatus.CORRECTED
        )
        _, d = ingest(store, "A", 40 * SECOND, prior=correction, status=ClaimStatus.RETRACTED)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        result = engine.source_reliability(query())
        assert result.sample_count == 4
        assert metric(result, "ERROR_CORRECTION").count == 1
        assert metric(result, "RETRACTION").count == 1
        assert metric(result, "UPDATE").count == 1
        assert metric(result, "ERROR_CORRECTION").ratio == 0.25
        assert result.source_classes == ("OFFICIAL_PRIMARY",)
        assert Q.LOW_SAMPLE in result.quality
        assert not hasattr(result, "score")
        validate(repo, result, deep=True)


def test_same_time_strict_prior_duplicates_and_future_correction_freeze():
    from eie.persistence.codec import dumps

    with SQLiteEventStore(":memory:") as store:
        first, row = ingest(store, "A", 10 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        assert (
            repo.index_claim(row.source_revision_id, repo.artifact(row.timing_id, ProcessingTiming))
            == row
        )
        q = query(10 * SECOND)
        frozen = engine.source_reliability(q)
        assert frozen.sample_count == 0
        old = engine.source_reliability(query(15 * SECOND))
        saved = dumps(old)
        ingest(store, "A", 20 * SECOND, prior=first, status=ClaimStatus.CORRECTED, value=99)
        assert dumps(engine.source_reliability(query(15 * SECOND))) == saved
        assert metric(engine.source_reliability(query(25 * SECOND)), "ERROR_CORRECTION").count == 1
        validate(repo, old, deep=True)


@pytest.mark.parametrize("family,confirmed", [("SAME", 0), ("OTHER", 1)])
def test_syndicated_families_are_not_independent(family, confirmed):
    with SQLiteEventStore(":memory:") as store:
        ingest(store, "A", 10 * SECOND, family="SAME")
        ingest(store, "B", 20 * SECOND, family=family)
        result = ReliabilityQueries(ReliabilityRepository(store)).source_reliability(query())
        assert result.corroboration.count == confirmed
        assert result.conflict.count == 0


def test_disagreement_has_no_ground_truth_and_class_stays_categorical():
    with SQLiteEventStore(":memory:") as store:
        ingest(store, "A", 10 * SECOND, value=100, publication=6 * SECOND)
        ingest(store, "B", 20 * SECOND, value=101, publication=10 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        a = engine.source_reliability(query())
        b = engine.source_reliability(replace(query(), provider="B"))
        assert a.conflict.count == b.conflict.count == 1
        assert a.source_classes == b.source_classes == ("OFFICIAL_PRIMARY",)
        assert Q.CONFLICT_GROUND_TRUTH_UNKNOWN in a.quality
        assert a.timeliness[3].median != b.timeliness[3].median
        compare = engine.compare(a, b)
        assert compare.left_id == a.artifact_id and compare.right_id == b.artifact_id


def test_explicit_rolling_periods_and_whole_boundary_ties():
    from eie.reliability.models import DAY

    with SQLiteEventStore(":memory:") as store:
        ingest(store, "A", 10 * SECOND, key="old")
        ingest(store, "A", 40 * DAY, key="new1")
        ingest(store, "A", 40 * DAY, key="new2")
        engine = ReliabilityQueries(ReliabilityRepository(store))
        cutoff = 41 * DAY
        all_prior = engine.source_reliability(query(cutoff))
        rolling = engine.source_reliability(
            query(cutoff, policy=SourceReliabilityPolicy(window=WindowKind.TRAILING_DAYS, days=30))
        )
        last = engine.source_reliability(
            query(
                cutoff,
                policy=SourceReliabilityPolicy(
                    window=WindowKind.TRAILING_OBSERVATIONS, observations=1
                ),
            )
        )
        assert (all_prior.sample_count, rolling.sample_count, last.sample_count) == (3, 2, 2)
        assert all_prior.artifact_id != rolling.artifact_id != last.artifact_id
        assert len(engine.history(DATASET.dataset_id, "A", "financial", cutoff)) == 3
