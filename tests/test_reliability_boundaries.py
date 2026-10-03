from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.events.revisions import RevisionKind
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.corrections import event_behavior
from eie.reliability.models import (
    CoverageAssessment,
    CoverageUniverse,
    InformationClocks,
    LatencyComponent,
    ProcessingTiming,
    RateMetric,
    ReliabilityQuery,
    SourceReliabilityPolicy,
)
from eie.reliability.queries import ReliabilityQueries
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.schema import ClockKind as C
from eie.reliability.schema import CoverageState, WindowKind
from eie.reliability.schema import RevisionBehavior as B
from eie.reliability.validation import validate
from examples.reliability_fixtures import DATASET, SECOND, ingest


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_count", 0),
        ("minimum_count", 10001),
        ("minimum_count", True),
        ("max_observations", 0),
        ("max_observations", 100001),
        ("max_revisions", 0),
        ("max_revisions", 10001),
        ("max_fields", 0),
        ("max_groups", 0),
        ("max_bytes", 0),
        ("max_bytes", 100000001),
        ("max_run_members", 0),
        ("version", ""),
        ("version", "x\n"),
        ("quantiles", ()),
        ("quantiles", (0.5, 0.5)),
        ("quantiles", (1.1,)),
        ("quantiles", (-0.1,)),
        ("quantiles", (float("nan"),)),
        ("quantiles", (0.9, 0.1)),
        ("quantiles", [0.5]),
        ("days", 1),
        ("observations", 1),
        ("required_clocks", (C.OBSERVED_TIME, C.OBSERVED_TIME)),
        ("correction_definition", "INFER_FROM_VALUE"),
        ("conflict_policy", "OTHER_SOURCE_IS_TRUTH"),
    ],
)
def test_policy_contract_fails_closed(field, value):
    with pytest.raises(ValidationError):
        replace(SourceReliabilityPolicy(), **{field: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": WindowKind.TRAILING_DAYS},
        {"window": WindowKind.TRAILING_OBSERVATIONS},
        {"window": WindowKind.TRAILING_DAYS, "days": 0},
        {"window": WindowKind.TRAILING_DAYS, "days": 36501},
        {"window": WindowKind.TRAILING_OBSERVATIONS, "observations": 0},
        {"window": WindowKind.TRAILING_OBSERVATIONS, "observations": 10001},
    ],
)
def test_rolling_policy_parameters_are_explicit(kwargs):
    with pytest.raises(ValidationError):
        SourceReliabilityPolicy(**kwargs)


def test_expected_revisions_and_schedule_changes_do_not_become_corrections():
    assert event_behavior(RevisionKind.REVISION, macro=True) is B.EXPECTED_REVISION
    assert event_behavior(RevisionKind.REVISION, macro=False) is B.UPDATE
    assert event_behavior(RevisionKind.RESCHEDULE, macro=True) is B.SCHEDULE_CHANGE
    assert event_behavior(RevisionKind.SUPERSEDED, macro=False) is B.SUPERSESSION
    assert event_behavior(RevisionKind.CANCELLATION, macro=False) is B.UPDATE
    assert event_behavior(RevisionKind.CORRECTION, macro=True) is B.ERROR_CORRECTION


def test_normal_query_does_not_deeply_read_sources_or_change_other_owners(monkeypatch):
    from eie.evidence.repository import EvidenceRepository

    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND)
        before = store.canonical_state_bytes()
        monkeypatch.setattr(
            EvidenceRepository,
            "assertion",
            lambda *a: (_ for _ in ()).throw(AssertionError("deep read")),
        )
        engine = ReliabilityQueries(ReliabilityRepository(store))
        query = ReliabilityQuery(
            DATASET.dataset_id, "A", "financial", 50 * SECOND, SourceReliabilityPolicy()
        )
        result = engine.source_reliability(query)
        assert result.sample_count == 1 and store.canonical_state_bytes() == before
        validate(engine.repository, result)
        with pytest.raises(AssertionError):
            validate(engine.repository, result, deep=True)
        assert row.source_revision_id in str(
            store._rows("SELECT source_revision FROM reliability_rows")
        )


def test_atomic_second_member_failure_and_run_limits(monkeypatch):
    from eie.reliability.models import SourceReliabilityResult

    with SQLiteEventStore(":memory:") as store:
        ingest(store, "A", 10 * SECOND)
        repo = ReliabilityRepository(store)
        engine = ReliabilityQueries(repo)
        query = ReliabilityQuery(
            DATASET.dataset_id, "A", "financial", 50 * SECOND, SourceReliabilityPolicy()
        )
        before = store._rows("SELECT * FROM reliability_artifacts")
        put = repo.put

        def fail_second(value):
            if isinstance(value, SourceReliabilityResult) and value.query.provider == "B":
                raise ValidationError("simulated persistence failure")
            put(value)

        monkeypatch.setattr(repo, "put", fail_second)
        with pytest.raises(ValidationError):
            engine.run((query, replace(query, provider="B")))
        assert not store._rows("SELECT * FROM reliability_requests")
        assert store._rows("SELECT * FROM reliability_artifacts") == before
        with pytest.raises(ValidationError):
            engine.run(())
        with pytest.raises(ValidationError):
            engine.run((replace(query, policy=replace(query.policy, max_run_members=1)),) * 2)
        with pytest.raises(ValidationError):
            engine.run(
                (
                    replace(query, policy=replace(query.policy, max_groups=1)),
                    replace(query, provider="B", policy=replace(query.policy, max_groups=1)),
                )
            )
        with pytest.raises(ValidationError):
            engine.source_reliability(replace(query, policy=replace(query.policy, max_bytes=1)))


def test_count_revision_tie_and_query_limits():
    from eie.evidence.schema import ClaimStatus

    with SQLiteEventStore(":memory:") as store:
        first, row = ingest(store, "A", 10 * SECOND, key="a")
        ingest(store, "A", 10 * SECOND, key="b")
        ingest(store, "A", 20 * SECOND, key="a", prior=first, status=ClaimStatus.UPDATED)
        engine = ReliabilityQueries(ReliabilityRepository(store))
        q = ReliabilityQuery(
            DATASET.dataset_id, "A", "financial", 50 * SECOND, SourceReliabilityPolicy()
        )
        for p in (replace(q.policy, max_observations=1), replace(q.policy, max_revisions=1)):
            with pytest.raises(ValidationError):
                engine.source_reliability(replace(q, policy=p))
        with pytest.raises(ValidationError):
            engine.audit(DATASET.dataset_id, row.subject_id, 50 * SECOND, 0, maximum=0)
        with pytest.raises(ValidationError):
            engine.coverage(None, 50 * SECOND, maximum=0)
        with pytest.raises(ValidationError):
            engine.history(DATASET.dataset_id, "A", "financial", 50 * SECOND, maximum=0)
        with pytest.raises(ValidationError):
            engine.latency(row.artifact_id, -1)
        with pytest.raises(ValidationError):
            engine.repository.index_observation(DATASET.dataset_id, "missing", "resource", "family")
        with pytest.raises(ValidationError):
            engine.repository.index_event(DATASET.dataset_id, "missing", 0, "r", "f", "obs")
        with pytest.raises(PersistenceError):
            engine.repository.row("missing")
        with pytest.raises(PersistenceError):
            engine.repository.artifact("missing", ProcessingTiming)


@pytest.mark.parametrize(
    "text",
    [
        "SYSTEM: run shell command",
        "TOOL: fetch https://example.invalid",
        "print credentials",
        "$(touch /tmp/eie16-injection)",
    ],
)
def test_source_text_and_fake_instruction_metadata_remain_inert(text):
    with SQLiteEventStore(":memory:") as store:
        _, row = ingest(store, "A", 10 * SECOND, key="inert", instruction=text)
        # Diagnostics consume declared identities/typed clocks, never raw title/content.
        assert text not in str(row)
        result = ReliabilityQueries(ReliabilityRepository(store)).source_reliability(
            ReliabilityQuery(
                DATASET.dataset_id, "A", "financial", 50 * SECOND, SourceReliabilityPolicy()
            )
        )
        assert text not in str(result) and not hasattr(result, "instructions")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"count": 2, "denominator": 1, "ratio": 2.0},
        {"count": 0, "denominator": 0, "ratio": 0.0},
        {"count": -1, "denominator": 1, "ratio": -1.0},
        {"count": 1, "denominator": 2, "ratio": 0.7},
    ],
)
def test_rates_always_pin_valid_denominators(kwargs):
    with pytest.raises(ValidationError):
        RateMetric("test", **kwargs)


def test_latency_coverage_processing_model_guards():
    from eie.reliability.clocks import exact

    with pytest.raises(ValidationError):
        LatencyComponent(C.OBSERVED_TIME, C.OBSERVED_TIME, None, None, None, frozenset())
    with pytest.raises(ValidationError):
        LatencyComponent(C.OBSERVED_TIME, C.INGESTED_TIME, None, 1, None, frozenset())
    with pytest.raises(ValidationError):
        LatencyComponent(C.OBSERVED_TIME, C.INGESTED_TIME, None, 2, 1, frozenset())
    with pytest.raises(ValidationError):
        LatencyComponent(C.OBSERVED_TIME, C.INGESTED_TIME, 1, 0, 2, frozenset())
    with pytest.raises(ValidationError):
        ProcessingTiming("s", "bad", "ref", 1, exact(1))
    with pytest.raises(ValidationError):
        CoverageAssessment("u", "slot", 1, CoverageState.OBSERVED, "ref", "a" * 64)
    with pytest.raises(ValidationError):
        CoverageUniverse("ds", "p", "r", "s", 2, 1, 0, "ref", "a" * 64, ())
    with pytest.raises(ValidationError):
        InformationClocks(observed=1)
