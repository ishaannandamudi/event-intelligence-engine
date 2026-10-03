import sqlite3
from dataclasses import replace

import pytest

from eie.analogues.engine import AnalogueEngine
from eie.analogues.models import AnalogueLimits, AnalogueQuery, AnalogueRunManifest
from eie.analogues.pool import collect
from eie.analogues.queries import AnalogueQueries
from eie.analogues.repository import AnalogueRepository
from eie.analogues.validation import validate
from eie.core.errors import PersistenceError, ValidationError
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.analogue_fixtures import fixture
from examples.event_studies import DAY
from examples.historical_analogues import POLICY, demonstrate


@pytest.fixture
def retained(tmp_path):
    value = fixture(tmp_path, POLICY)
    try:
        yield value
    finally:
        value.store.close()


def query(data, **kwargs):
    t = data.rows[-1]
    return replace(
        AnalogueQuery(
            t.row_id,
            t.event_id,
            t.revision,
            t.dataset_id,
            t.feature_cutoff_ns,
            t.available_ns,
            t.policy_id,
            data.spec,
        ),
        **kwargs,
    )


def test_example_real_studies_freeze_reaction_and_restart(tmp_path):
    value = demonstrate(tmp_path)
    assert value["prior_count"] == 7 and value["restart_byte_identical"]
    assert value["frozen_members_unchanged"]
    assert value["actual_available_ns"] > value["source_cutoff_ns"]
    assert value["reaction_available_ns"] > value["actual_available_ns"]
    assert value["information_surprise"]["raw"] is None
    assert value["counterfactual_impact_id"].startswith("impact_")


def test_frozen_sources_reference_small_rows_and_do_not_recompute_eie13(retained, monkeypatch):
    from eie.intelligence import collection
    from eie.intelligence.engine import IntelligenceEngine

    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary retrieval rebuilt intelligence")

    monkeypatch.setattr(collection, "collect", forbidden)
    monkeypatch.setattr(IntelligenceEngine, "compute", forbidden)
    repo = retained.repository
    result = AnalogueEngine(repo).analogues(query(retained))
    assert result.distribution.count == 7
    assert repo.get(result.result_id) == result
    assert validate(repo, result.result_id).state.value != "INVALID"
    assert b"EventIntelligenceResult" not in dumps(result)
    assert b"MarketBar" not in dumps(result)
    assert b"CanonicalEvent" not in dumps(result)
    assert len(dumps(result)) < 50000


def test_query_apis_enforce_actual_availability_and_restart(retained):
    repo = retained.repository
    result = AnalogueEngine(repo).analogues(query(retained))
    api = AnalogueQueries(repo)
    assert api.analogues_as_of(result.query.known_as_of_ns) == ()
    assert api.analogues_as_of(result.query.available_ns) == (result,)
    assert api.analogues_as_of(
        result.query.available_ns,
        event_id=result.query.target_event_id,
        dataset_id=result.query.dataset_id,
        policy_id=result.query.policy_id,
    ) == (result,)
    assert api.analogue_distribution(result.result_id) == result.distribution
    assert api.analogue_uncertainty(result.result_id) == result.uncertainty
    assert api.candidate_explanations(result.result_id) == result.candidates
    assert api.analogues("absent") is None
    assert api.analogue_distribution("absent") is None
    assert api.analogue_uncertainty("absent") == api.candidate_explanations("absent") == ()
    assert api.reaction_surprise("absent") is None and api.run("absent") is None
    missing = repo.attach_reaction(result.result_id, None, result.query.available_ns)
    assert api.reaction_surprise(missing.reaction_id) == missing
    assert missing.state.value == "UNAVAILABLE"
    assert AnalogueEngine(repo).analogues(query(retained)) == result
    repo.append_result(result)
    assert validate(repo, result.result_id, deep=True).state.value == "VALID_WITH_WARNINGS"


@pytest.mark.parametrize("kind", ["pool", "selection", "responses", "features", "bytes"])
def test_declared_bounds_fail_without_arbitrary_truncation(retained, kind):
    limits = replace(
        AnalogueLimits(),
        **{"pool_candidates": 2}
        if kind == "pool"
        else {"selected": 2}
        if kind == "selection"
        else {"responses": 2}
        if kind == "responses"
        else {"features": 1}
        if kind == "features"
        else {"query_output_bytes": 100},
    )
    repo = AnalogueRepository(retained.store, limits)
    with pytest.raises(ValidationError):
        AnalogueEngine(repo).analogues(query(retained))
    assert retained.store._rows("SELECT id FROM analogue_results") == []


def test_response_observation_bound_counts_entire_collected_pool_not_only_top_k(retained):
    bounded = AnalogueRepository(retained.store, replace(AnalogueLimits(), responses=2))
    with pytest.raises(ValidationError, match="response"):
        AnalogueEngine(bounded).analogues(query(retained, top_k=1))
    assert retained.store._rows("SELECT id FROM analogue_results") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_revision", 2),
        ("target_event_id", "absent"),
        ("target_row_id", "absent"),
        ("known_as_of_ns", 1),
        ("available_ns", 19 * DAY),
    ],
)
def test_query_must_pin_exact_visible_target(retained, field, value):
    q = query(retained, **{field: value})
    with pytest.raises((PersistenceError, ValidationError)):
        AnalogueEngine(retained.repository).analogues(q)


def test_source_feature_recalculation_rejects_forged_values(retained):
    repo, row = retained.repository, retained.rows[0]
    repo.append_feature(row)
    assert repo.feature(row.row_id) == row
    forged = replace(row, values=(row.values[0], replace(row.values[1], value=999)))
    with pytest.raises(ValidationError, match="recalculation"):
        repo.append_feature(forged)
    assert repo.feature(forged.row_id) is None
    with pytest.raises(ValidationError):
        repo.index_features("absent", row.policy_id)
    with pytest.raises(PersistenceError):
        repo.index_features(row.intelligence_id, "absent")
    assert repo.feature("absent") is None and repo.response("absent") is None


def test_frozen_pool_blocks_late_backdating_and_allows_future_lineage(retained):
    repo = retained.repository
    frozen = AnalogueEngine(repo).analogues(query(retained))
    row = retained.rows[0]
    contexts = tuple(replace(c, version="new") for c in row.contexts)
    with pytest.raises(ValidationError, match="backdate"):
        repo.index_features(row.intelligence_id, row.policy_id, contexts)
    # A different mathematical policy creates separate immutable lineage.
    p = replace(POLICY, version="new-policy")
    repo.append_policy(p)
    alternate = repo.index_features(row.intelligence_id, p.policy_id, row.contexts)
    assert alternate.row_id != row.row_id
    assert dumps(repo.get(frozen.result_id)) == dumps(frozen)
    assert validate(repo, frozen.result_id, deep=True).state.value != "INVALID"


def test_query_output_bound_applies_to_existing_results_and_aggregate_history(retained):
    repo = retained.repository
    a = AnalogueEngine(repo).analogues(query(retained, top_k=5))
    b = AnalogueEngine(repo).analogues(query(retained, top_k=6))
    small = AnalogueRepository(retained.store, replace(AnalogueLimits(), query_output_bytes=100))
    with pytest.raises(ValidationError, match="output"):
        small.get(a.result_id)
    limit = max(len(dumps(a)), len(dumps(b))) + 1
    bounded = AnalogueRepository(
        retained.store, replace(AnalogueLimits(), query_output_bytes=limit)
    )
    assert bounded.get(a.result_id) == a
    with pytest.raises(ValidationError, match="history output"):
        AnalogueQueries(bounded).analogues_as_of(a.query.available_ns)


def test_late_target_response_does_not_change_frozen_members(retained):
    repo = retained.repository
    frozen = AnalogueEngine(repo).analogues(query(retained))
    before = dumps(frozen)
    target, study = retained.rows[-1], retained.studies[-1]
    response = repo.index_response(target.row_id, study.study_id, retained.spec)
    assert repo.index_response(target.row_id, study.study_id, retained.spec) == response
    surprise = repo.attach_reaction(frozen.result_id, response.response_id, target.available_ns + 1)
    assert repo.reaction(surprise.reaction_id) == surprise
    assert AnalogueEngine(repo).analogues(query(retained)) == frozen
    assert dumps(repo.get(frozen.result_id)) == before
    assert validate(repo, frozen.result_id, deep=True).state.value != "INVALID"
    with pytest.raises(ValidationError):
        repo.attach_reaction("absent", None, target.available_ns)


def test_run_members_pins_and_query_limit(retained):
    repo = retained.repository
    a = AnalogueEngine(repo).analogues(query(retained, top_k=5))
    b = AnalogueEngine(repo).analogues(query(retained, top_k=6))
    results = tuple(sorted((a, b), key=lambda r: r.result_id))
    run = AnalogueRunManifest(
        tuple(r.result_id for r in results),
        tuple(r.fingerprint for r in results),
        a.query.dataset_id,
        a.query.available_ns,
    )
    repo.append_run(run)
    repo.append_run(run)
    assert repo.run(run.run_id) == run
    with pytest.raises(ValidationError):
        AnalogueQueries(repo).analogues_as_of(run.available_ns, limit=1)
    with pytest.raises(ValidationError):
        repo.append_run(replace(run, result_fingerprints=("b" * 64,) * 2))
    with pytest.raises(ValidationError):
        repo.append_run(replace(run, available_ns=run.available_ns - 1))
    assert repo.run("absent") is None
    assert validate(repo, "absent").state.value == "INVALID"


@pytest.mark.parametrize(
    "table",
    [
        "analogue_policies",
        "analogue_features",
        "analogue_entities",
        "analogue_contexts",
        "analogue_responses",
        "analogue_queries",
        "analogue_results",
        "analogue_members",
        "analogue_selected",
        "analogue_uncertainty",
        "analogue_seals",
    ],
)
def test_immutable_tables_reject_update_and_delete(retained, table):
    AnalogueEngine(retained.repository).analogues(query(retained))
    with pytest.raises(sqlite3.IntegrityError):
        retained.store._connection.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize(
    "table,column",
    [
        ("analogue_policies", "fingerprint"),
        ("analogue_features", "fingerprint"),
        ("analogue_features", "event_available"),
        ("analogue_contexts", "category"),
        ("analogue_responses", "fingerprint"),
        ("analogue_queries", "fingerprint"),
        ("analogue_results", "fingerprint"),
        ("analogue_selected", "fingerprint"),
        ("analogue_members", "fingerprint"),
        ("analogue_uncertainty", "state"),
    ],
)
def test_scalar_and_membership_corruption_fail_closed(retained, table, column):
    repo = retained.repository
    frozen = AnalogueEngine(repo).analogues(query(retained))
    retained.store._connection.execute(f"DROP TRIGGER immutable_{table}_update")
    retained.store._connection.execute(
        f"UPDATE {table} SET {column}=?", ("corrupt" if column != "event_available" else 0,)
    )
    with pytest.raises(PersistenceError):
        repo.get(frozen.result_id)
    assert validate(repo, frozen.result_id).state.value == "INVALID"


def test_forged_result_must_recalculate_exactly(retained):
    from eie.analogues.retrieval import retrieve

    repo, q = retained.repository, query(retained)
    with retained.store.transaction():
        value = retrieve(collect(repo, q, POLICY), q, POLICY)
    with pytest.raises(ValidationError):
        repo.append_result(replace(value, distribution=replace(value.distribution, mean=999)))
    assert repo.get(value.result_id) is None
    repo.append_result(value)
    assert repo.get(value.result_id) == value


def test_migration_17_to_18_preserves_prior_bytes_and_rolls_back(tmp_path, monkeypatch):
    from eie.persistence import migrations

    original = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", original[:8])
    path = tmp_path / "legacy.sqlite"
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "1.7"
        store._connection.execute("INSERT INTO sessions VALUES ('old','1.0',?)", (b"unchanged",))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*original[:8], (*original[8], "invalid SQL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT schema_version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone() == ("1.7",)
    assert not connection.execute(
        "SELECT name FROM sqlite_master WHERE name='analogue_features'"
    ).fetchall()
    connection.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", original)
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "2.0"
        assert store._rows("SELECT payload FROM sessions WHERE namespace='old'") == [
            (b"unchanged",)
        ]
