from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.bitemporal import calculate
from eie.reliability.clocks import exact
from eie.reliability.models import DAY
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import ReliabilityQuality as Q
from eie.reliability.validation import validate
from examples.reliability_fixtures import DATASET, SECOND, ingest


def test_valid_time_does_not_make_unknown_disclosure_visible_and_replays_revision():
    from eie.evidence.schema import ClaimStatus

    with SQLiteEventStore(":memory:") as store:
        first, row = ingest(store, "A", 10 * SECOND, valid=0)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        early = engine.audit(DATASET.dataset_id, row.subject_id, 9 * SECOND, 0)
        assert not early.entries
        old = engine.audit(DATASET.dataset_id, row.subject_id, 15 * SECOND, 0)
        saved = dumps(old)
        _, later = ingest(
            store, "A", 20 * SECOND, prior=first, value=99, status=ClaimStatus.CORRECTED, valid=0
        )
        current = engine.audit(DATASET.dataset_id, row.subject_id, 25 * SECOND, 0)
        assert old.selected_row_ids == (row.artifact_id,)
        assert current.selected_row_ids == (later.artifact_id,)
        assert len(current.entries) == 2 and current.entries[1].known_ns == 20 * SECOND
        assert dumps(engine.audit(DATASET.dataset_id, row.subject_id, 15 * SECOND, 0)) == saved
        validate(repo, old, deep=True)
        validate(repo, current, deep=True)
        with pytest.raises(PersistenceError):
            validate(repo, replace(current, selected_row_ids=(row.artifact_id,)))


def test_half_open_validity_unknown_validity_and_coarse_interval():
    from eie.evidence.claims import TimePrecision, TimeValue

    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND)
        finite = replace(row, valid_to_ns=20 * SECOND)
        result = calculate(DATASET.dataset_id, row.subject_id, 30 * SECOND, 20 * SECOND, (finite,))
        assert not result.selected_row_ids
        unknown = replace(row, clocks=replace(row.clocks, valid=None))
        result = calculate(DATASET.dataset_id, row.subject_id, 30 * SECOND, 0, (unknown,))
        assert result.unknown_validity_row_ids == (unknown.artifact_id,)
        assert Q.UNKNOWN_VALIDITY in result.quality and not result.selected_row_ids
        coarse = replace(
            row,
            clocks=replace(
                row.clocks,
                valid=TimeValue(TimePrecision.DATE_ONLY, "1970-01-01"),
                coarse_timezone="UTC",
            ),
        )
        assert calculate(
            DATASET.dataset_id, row.subject_id, 30 * SECOND, DAY - 1, (coarse,)
        ).selected_row_ids
        assert not calculate(
            DATASET.dataset_id, row.subject_id, 30 * SECOND, DAY, (coarse,)
        ).selected_row_ids


def test_reconstruction_keeps_actual_local_observation_and_prior_exclusion():
    from eie.reliability.models import ReliabilityQuery, SourceReliabilityPolicy
    from eie.reliability.reliability import calculate as metrics

    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 100 * SECOND)
        reconstructed = replace(
            row,
            known_ns=10 * SECOND,
            reconstructed=True,
            clocks=replace(row.clocks, source_available=exact(10 * SECOND)),
        )
        q = ReliabilityQuery(
            DATASET.dataset_id, "A", "financial", 50 * SECOND, SourceReliabilityPolicy()
        )
        assert metrics(q, (reconstructed,), ()).sample_count == 0
        assert not calculate(
            DATASET.dataset_id, row.subject_id, 50 * SECOND, 0, (reconstructed,)
        ).entries
        audit = calculate(DATASET.dataset_id, row.subject_id, 110 * SECOND, 0, (reconstructed,))
        assert audit.entries[0].observed.timestamp_ns == 98 * SECOND
        assert audit.entries[0].known_ns == 10 * SECOND
        assert Q.RECONSTRUCTION_LIMITATION in audit.quality


def test_later_instrumentation_preserves_original_visibility_and_sample_clock():
    from eie.reliability.models import ProcessingTiming, ReliabilityQuery, SourceReliabilityPolicy

    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        early = engine.audit(DATASET.dataset_id, row.subject_id, 10 * SECOND, 0)
        assert len(early.entries) == 1 and early.entries[0].known_ns == 10 * SECOND
        base = repo.row(early.selected_row_ids[0])
        assert base.clocks.snapshot is None
        q = ReliabilityQuery(
            DATASET.dataset_id, "A", "financial", 11 * SECOND, SourceReliabilityPolicy()
        )
        initial = engine.source_reliability(q)
        assert initial.sample_count == 1 and initial.timeliness[6].count == 0
        original = dumps(initial)
        timing = ProcessingTiming(
            row.source_revision_id,
            row.source_fingerprint,
            "later-instrumentation",
            30 * SECOND,
            exact(10 * SECOND),
            exact(30 * SECOND),
        )
        updated = repo.index_claim(row.source_revision_id, timing)
        assert (
            updated.sample_available_ns == 10 * SECOND
            and updated.metric_available_ns == 30 * SECOND
        )
        from dataclasses import replace

        current = engine.source_reliability(replace(q, known_as_of_ns=40 * SECOND))
        assert current.sample_count == 1 and current.timeliness[6].median == 20 * SECOND
        assert dumps(engine.source_reliability(q)) == original
        validate(repo, initial, deep=True)
        validate(repo, current, deep=True)
