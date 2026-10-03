from dataclasses import replace

import pytest

from eie.analogues.engine import AnalogueEngine
from eie.analogues.models import AnaloguePolicy, AnalogueQuery, ContextFeature
from eie.analogues.pool import target_exclusions
from eie.analogues.repository import AnalogueRepository
from eie.analogues.schema import SectorCondition, TypeCondition
from eie.analogues.validation import validate
from eie.core.errors import ValidationError
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.analogue_fixtures import fixture
from examples.historical_analogues import POLICY
from tests.test_analogue_persistence import query


def test_future_high_similarity_context_and_intelligence_do_not_change_old_result(tmp_path):
    data = fixture(tmp_path, POLICY, days=tuple(range(2, 11)))
    try:
        repo, target, future = data.repository, data.rows[-2], data.rows[-1]
        q = AnalogueQuery(
            target.row_id,
            target.event_id,
            target.revision,
            target.dataset_id,
            target.feature_cutoff_ns,
            target.available_ns,
            POLICY.policy_id,
            data.spec,
        )
        frozen = AnalogueEngine(repo).analogues(q)
        before = dumps(frozen)
        changed_context = tuple(
            replace(c, value=target.values[-1].value, version="future")
            if c.dimension == "volatility"
            else c
            for c in future.contexts
        )
        similar_future = repo.index_features(
            future.intelligence_id, POLICY.policy_id, changed_context
        )
        assert similar_future.row_id not in {m.row_id for m in frozen.pool}
        assert similar_future.row_id not in {c.row_id for c in frozen.candidates}
        assert dumps(AnalogueEngine(repo).analogues(q)) == before
        assert validate(repo, frozen.result_id, deep=True).state.value != "INVALID"
    finally:
        data.store.close()


def test_concurrent_future_index_append_cannot_mix_frozen_pool_snapshot(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from eie.analogues import engine
    from eie.analogues.pool import collect

    data = fixture(tmp_path, POLICY, days=tuple(range(2, 11)))
    target, future = data.rows[-2:]
    q = query(
        data,
        target_row_id=target.row_id,
        target_event_id=target.event_id,
        target_revision=target.revision,
        known_as_of_ns=target.feature_cutoff_ns,
        available_ns=target.available_ns,
    )
    writer = SQLiteEventStore(tmp_path / "studies.sqlite")
    started, finished = Event(), Event()
    original = collect
    contexts = tuple(replace(c, version="concurrent-future") for c in future.contexts)

    def append_future():
        started.set()
        row = AnalogueRepository(writer).index_features(
            future.intelligence_id, future.policy_id, contexts
        )
        finished.set()
        return row

    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = None

            def snapshot(repo, query, policy):
                nonlocal pending
                pending = workers.submit(append_future)
                assert started.wait(5)
                assert not finished.is_set()
                return original(repo, query, policy)

            monkeypatch.setattr(engine, "collect", snapshot)
            frozen = AnalogueEngine(data.repository).analogues(q)
            row = pending.result(timeout=10)
            assert finished.is_set() and row.row_id not in {m.row_id for m in frozen.pool}
            assert data.repository.get(frozen.result_id) == frozen
            assert validate(data.repository, frozen.result_id, deep=True).state.value != "INVALID"
    finally:
        writer.close()
        data.store.close()


@pytest.mark.parametrize("condition", list(TypeCondition))
def test_indexed_family_and_type_blocks_have_explicit_counts(tmp_path, condition):
    p = replace(
        POLICY,
        type_condition=condition,
        allowed_types=("EARNINGS.RELEASE",) if condition is TypeCondition.ALLOWED_SET else (),
    )
    data = fixture(tmp_path, p)
    try:
        repo = data.repository
        result = AnalogueEngine(repo).analogues(query(data))
        assert result.diagnostics.total_prior_events == 7
        assert result.diagnostics.blocked_candidates == 8
        assert result.distribution.count == 7
        assert all(c.event_available_ns < result.query.known_as_of_ns for c in result.candidates)
        assert all(c.response_available_ns < result.query.known_as_of_ns for c in result.candidates)
    finally:
        data.store.close()


@pytest.mark.parametrize("missing_sector", [True, False])
def test_sector_condition_never_silently_broadens(tmp_path, missing_sector):
    data = fixture(tmp_path, POLICY)
    try:
        repo, target = data.repository, data.rows[-1]
        contexts = tuple(c for c in target.contexts if c.dimension != "sector")
        if not missing_sector:
            contexts = tuple(
                sorted(
                    (
                        *contexts,
                        ContextFeature(
                            "sector",
                            "OTHER",
                            "S",
                            "1",
                            target.feature_cutoff_ns,
                            target.feature_cutoff_ns,
                        ),
                    ),
                    key=lambda c: c.dimension,
                )
            )
        alternate = repo.index_features(target.intelligence_id, target.policy_id, contexts)
        result = AnalogueEngine(repo).analogues(query(data, target_row_id=alternate.row_id))
        assert not result.candidates and result.diagnostics.blocked_candidates <= 1
        # Cross-sector retrieval requires an explicit separate policy.
        broad = replace(POLICY, sector_condition=SectorCondition.UNCONDITIONED)
        repo.append_policy(broad)
        for old, study in zip(data.rows[:-1], data.studies[:-1], strict=True):
            broad_row = repo.index_features(old.intelligence_id, broad.policy_id, old.contexts)
            repo.index_response(broad_row.row_id, study.study_id, data.spec)
        new_target = repo.index_features(target.intelligence_id, broad.policy_id, contexts)
        broad_query = query(data, target_row_id=new_target.row_id, policy_id=broad.policy_id)
        assert len(AnalogueEngine(repo).analogues(broad_query).candidates) == 7
    finally:
        data.store.close()


@pytest.mark.parametrize("dimension", ["sector", "volatility", "absent"])
def test_indexed_context_blocking_is_explicit_and_bounded(tmp_path, dimension):
    p = replace(POLICY, blocking_dimensions=(dimension,))
    data = fixture(tmp_path, p)
    try:
        if dimension == "absent":
            with pytest.raises(ValidationError, match="blocking"):
                AnalogueEngine(data.repository).analogues(query(data))
        else:
            result = AnalogueEngine(data.repository).analogues(query(data))
            assert result.diagnostics.blocked_candidates == (8 if dimension == "sector" else 1)
    finally:
        data.store.close()


def test_same_issuer_is_optional_explicit_index_filter(tmp_path):
    p = replace(POLICY, same_issuer=True)
    data = fixture(tmp_path, p)
    try:
        result = AnalogueEngine(data.repository).analogues(query(data))
        assert result.distribution.count == 7
        assert all(c.row_id in {r.row_id for r in data.rows} for c in result.candidates)
    finally:
        data.store.close()


def test_real_eie11_alias_run_excludes_prior_alias_without_mutating_resolution():
    from eie.analogues.models import ResponseSpec
    from eie.analogues.schema import ResponseMetric
    from eie.intelligence.engine import IntelligenceEngine
    from eie.intelligence.models import IntelligenceRequest
    from eie.intelligence.policy import IntelligencePolicy
    from eie.intelligence.repository import IntelligenceRepository
    from tests.evidence_helpers import DATASET, item, setup, source

    with SQLiteEventStore(":memory:") as store:
        _, evidence_engine, evidence_queries = setup(store)
        a, b = source("ALPHA"), source("BETA")
        first, second = item(a, 100), item(b, 200)
        evidence_engine.ingest(a, first)
        intel_repo = IntelligenceRepository(store)
        ip = IntelligencePolicy()
        intel_repo.append_policy(ip)
        one = IntelligenceEngine(intel_repo).compute(
            IntelligenceRequest(
                first.events[0].event_id, 1, DATASET.dataset_id, 100, 100, ip.policy_id
            )
        )
        evidence_engine.ingest(b, second)
        resolution = evidence_queries.event_resolution(200)
        two = IntelligenceEngine(intel_repo).compute(
            IntelligenceRequest(
                second.events[0].event_id, 1, DATASET.dataset_id, 200, 200, ip.policy_id
            )
        )
        repo = AnalogueRepository(store)
        p = AnaloguePolicy(sector_condition=SectorCondition.UNCONDITIONED)
        repo.append_policy(p)
        first_row = repo.index_features(one.intelligence_id, p.policy_id)
        second_row = repo.index_features(two.intelligence_id, p.policy_id)
        q = AnalogueQuery(
            second_row.row_id,
            second_row.event_id,
            1,
            DATASET.dataset_id,
            200,
            200,
            p.policy_id,
            ResponseSpec(ResponseMetric.RAW_RETURN, "a" * 64, 1),
            event_resolution_id=resolution.run_id,
        )
        before = dumps(resolution)
        result = AnalogueEngine(repo).analogues(q)
        assert first_row.event_id in result.excluded_event_ids
        assert not result.candidates
        assert dumps(evidence_queries.event_resolution(200)) == before
        with pytest.raises(ValidationError):
            target_exclusions(repo, replace(q, known_as_of_ns=150), second_row)


def test_frozen_hash_seed_result_and_future_insert_order_are_deterministic(tmp_path):
    import os
    import subprocess
    import sys

    data = fixture(tmp_path, POLICY)
    try:
        frozen = AnalogueEngine(data.repository).analogues(query(data))
        identity, expected = frozen.result_id, frozen.fingerprint
    finally:
        data.store.close()
    program = (
        "from eie.persistence.sqlite import SQLiteEventStore; "
        "from eie.analogues.repository import AnalogueRepository; import sys; "
        "s=SQLiteEventStore(sys.argv[1]); "
        "print(AnalogueRepository(s).get(sys.argv[2]).fingerprint); s.close()"
    )
    for seed in ("1", "23", "random"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path / "studies.sqlite"), identity],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.stdout.strip() == expected
