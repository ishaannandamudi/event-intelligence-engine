"""Hash-seed determinism, later resolution lineage, and independent validation."""

import os
import subprocess
import sys
from dataclasses import replace

from eie.evidence.dedup import resolve_events
from eie.evidence.repository import EvidenceRepository
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.validation import validate
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.intelligence_fixtures import DATASET, batch, source
from tests.intelligence_helpers import compute, ingest


def test_representative_result_is_identical_across_python_hash_seeds():
    code = """
from eie.persistence.sqlite import SQLiteEventStore
from examples.intelligence_fixtures import batch, source
from tests.intelligence_helpers import compute, ingest
with SQLiteEventStore(':memory:') as store:
    p=source()
    row=batch(p,20)
    ingest(store,p,row)
    print(compute(store,row).result_fingerprint)
"""
    outputs = tuple(
        subprocess.check_output(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
        )
        for seed in ("1", "17", "321")
    )
    assert len(set(outputs)) == 1


def test_later_eie11_run_does_not_replace_pinned_absence():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        repo = IntelligenceRepository(store)
        result = compute(store, b)
        assert result.manifest.request.event_resolution_id is None
        original = dumps(result)
        er = EvidenceRepository(store)
        run, candidates = resolve_events((), DATASET.dataset_id, 20)
        with store.transaction():
            er.append_event_run(run, candidates)
        assert dumps(repo.get(result.intelligence_id)) == original
        assert validate(repo, result.intelligence_id).state.value != "INVALID"
        repo.append(result)


def test_validation_recalculates_even_coherent_typed_result(monkeypatch):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        repo = IntelligenceRepository(store)
        result = compute(store, b)
        forged = replace(result, quality=("COHERENT_BUT_UNSUPPORTED",))
        monkeypatch.setattr(repo, "get", lambda _: forged)
        check = validate(repo, result.intelligence_id)
        assert check.state.value == "INVALID"
        assert check.reasons == ("SOURCE_COMPONENT_RECALCULATION_MISMATCH",)
