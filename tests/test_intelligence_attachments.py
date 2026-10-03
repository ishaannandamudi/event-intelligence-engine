"""Real historical impact attachments preserve original intelligence and source clocks."""

from dataclasses import replace
from pathlib import Path

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.impact.models import ImpactQuality
from eie.impact.repository import ImpactRepository
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.queries import IntelligenceQueries
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.uncertainty import impact_uncertainty
from eie.intelligence.validation import validate
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.intelligence_demo import demonstrate, historical_audit


@pytest.fixture
def audit_store(tmp_path):
    display = historical_audit(tmp_path)
    with SQLiteEventStore(tmp_path / "studies.sqlite") as store:
        repo = IntelligenceRepository(store)
        result = repo.get(store._rows("SELECT id FROM ei_results")[0][0])
        audit = repo.audit(store._rows("SELECT id FROM ei_audits")[0][0])
        impact = ImpactRepository(store).get(display["impact_reference"])
        yield store, repo, result, audit, impact


def test_offline_example_includes_real_surprise_impact_and_restart(tmp_path):
    shown = demonstrate(Path(tmp_path))
    assert shown["restart_byte_identical"]
    assert shown["impact_does_not_change_ex_ante_materiality"]
    assert shown["audit_mode"] == "EX_POST_AUDIT"
    assert "CONSENSUS_MISSING" in shown["surprise_reasons"]
    assert len({c[0] for c in shown["novelty"]}) == 6


def test_historical_acquisition_and_audit_do_not_leak_into_baseline(audit_store):
    _, repo, result, audit, impact = audit_store
    original = dumps(result)
    assert result.baseline_cutoff_ns < result.manifest.request.available_ns
    q = IntelligenceQueries(repo, result.manifest.request.dataset_id)
    assert q.intelligence_as_of(result.event_id, result.baseline_cutoff_ns) is None
    assert q.intelligence_as_of(result.event_id, result.manifest.request.available_ns) == result
    assert (
        repo.attach_impact(result.intelligence_id, (impact.impact_id,), audit.available_ns) == audit
    )
    assert dumps(repo.get(result.intelligence_id)) == original
    assert validate(repo, result.intelligence_id).state.value != "INVALID"
    assert repo.audit("absent") is None
    for identity, ids, available in (
        ("absent", (impact.impact_id,), audit.available_ns),
        (result.intelligence_id, (), audit.available_ns),
        (result.intelligence_id, ("absent",), audit.available_ns),
        (result.intelligence_id, (impact.impact_id,), result.baseline_cutoff_ns),
    ):
        with pytest.raises(ValidationError):
            repo.attach_impact(identity, ids, available)
    with pytest.raises(ValidationError):
        IntelligenceEngine(repo).compute(
            replace(result.manifest.request, impact_ids=(impact.impact_id,))
        )
    with pytest.raises(PersistenceError):
        IntelligenceEngine(repo).compute(replace(result.manifest.request, impact_ids=("absent",)))


def test_already_available_impact_is_explicit_ex_post_and_separate(audit_store):
    _, repo, result, _, impact = audit_store
    t = max(impact.available_ns, result.manifest.request.available_ns) + 1
    request = replace(result.manifest.request, known_as_of_ns=t, available_ns=t)
    original = IntelligenceEngine(repo).compute(request)
    attached = IntelligenceEngine(repo).compute(replace(request, impact_ids=(impact.impact_id,)))
    assert attached.mode.value == "EX_POST_AUDIT"
    assert original.mode.value == "EX_ANTE_INTELLIGENCE"
    assert attached.novelty == original.novelty
    assert attached.materiality == original.materiality
    assert attached.static_importance == original.static_importance
    assert next(c for c in attached.uncertainty if c.kind.value == "IMPACT").input_ids == (
        impact.impact_id,
    )


@pytest.mark.parametrize("quality", list(ImpactQuality))
def test_all_actual_impact_quality_diagnostics_stay_in_impact_component(audit_store, quality):
    _, _, _, _, impact = audit_store
    diagnostic = impact_uncertainty((replace(impact, quality=frozenset({quality})),))
    assert "EIE9_" + quality.value in diagnostic.reasons
    assert diagnostic.kind.value == "IMPACT"


def test_audit_immutability_and_corrupt_index_refused(audit_store):
    store, repo, _, audit, _ = audit_store
    for action in ("UPDATE", "DELETE"):
        with pytest.raises(PersistenceError), store.transaction():
            store._connection.execute(
                "UPDATE ei_audits SET available=available"
                if action == "UPDATE"
                else "DELETE FROM ei_audits"
            )
    store._connection.execute("DROP TRIGGER immutable_ei_audits_update")
    store._connection.execute("UPDATE ei_audits SET fingerprint='corrupt'")
    with pytest.raises(PersistenceError):
        repo.audit(audit.audit_id)
