from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.corporate.types import EarningsPeriod
from eie.evidence.schema import ClaimStatus
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import InteractionPolicy, InteractionQuery, InteractionWindow
from eie.interactions.queries import InteractionQueries
from eie.interactions.repository import InteractionRepository
from eie.interactions.schema import OrderingBasis
from eie.interactions.validation import validate
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from tests.evidence_helpers import DATASET, item, setup, source


def test_real_eie11_merge_then_split_changes_only_new_snapshot():
    with SQLiteEventStore(":memory:") as store:
        evidence, engine, api = setup(store)
        a, b = source("ALPHA"), source("BETA")
        first, second = item(a, 100), item(b, 200)
        engine.ingest(a, first)
        engine.ingest(b, second)
        repo = InteractionRepository(store)
        p = InteractionPolicy(window=InteractionWindow(basis=OrderingBasis.SCHEDULED_TIME))
        repo.append_policy(p)
        repo.index_event(DATASET.dataset_id, first.events[0].event_id, 100)
        repo.index_event(DATASET.dataset_id, second.events[0].event_id, 200)
        merged = api.event_resolution(200)
        with store.transaction():
            evidence.append_event_run(merged, api.duplicate_candidates(200))
        q = InteractionQuery(DATASET.dataset_id, second.events[0].event_id, 200, p.artifact_id)
        frozen = InteractionEngine(repo).interaction_context(q)
        assert not frozen.collision_ids
        assert len(repo.manifest(frozen.artifact_id).collision_row_ids) == 1
        before = dumps(frozen)
        fourth = item(b, 400, prior=second, status=ClaimStatus.CORRECTED)
        fourth = replace(
            fourth,
            descriptors=(
                replace(
                    fourth.descriptors[0],
                    period=EarningsPeriod(2026, 2, "2026-06-30"),
                    corporate_identity=None,
                ),
            ),
        )
        engine.ingest(b, fourth)
        repo.index_event(DATASET.dataset_id, fourth.events[0].event_id, 400)
        split = api.event_resolution(400)
        with store.transaction():
            evidence.append_event_run(split, api.duplicate_candidates(400))
        new = InteractionEngine(repo).interaction_context(replace(q, known_as_of_ns=400))
        assert len(new.collision_ids) == 1
        assert dumps(repo.context(frozen.artifact_id)) == before
        validate(repo, new.artifact_id, deep=True)
        with pytest.raises(ValidationError, match="identity"):
            InteractionEngine(repo).interaction_context(
                replace(q, event_resolution_id=split.run_id)
            )
        seq = InteractionQueries(repo).sequence(
            DATASET.dataset_id,
            first.events[0].identity.scope.identifiers[0],
            0,
            200,
            200,
            p.artifact_id,
            OrderingBasis.KNOWLEDGE_TIME,
            event_resolution_id=merged.run_id,
        )
        assert len(seq.row_ids) == 1
        with pytest.raises(ValidationError):
            InteractionQueries(repo).sequence(
                DATASET.dataset_id,
                "iss_fixture",
                0,
                200,
                200,
                p.artifact_id,
                event_resolution_id=split.run_id,
            )


def test_eie11_time_precision_survives_indexing_and_extraction_delay():
    from eie.evidence.claims import TimePrecision, TimeValue

    with SQLiteEventStore(":memory:") as store:
        _, engine, _ = setup(store)
        provider = source("PRIMARY")
        value = TimeValue(TimePrecision.DATE_ONLY, "1970-01-01")
        batch = item(provider, 100, value)
        engine.ingest(provider, batch)
        repo = InteractionRepository(store)
        row = repo.index_event(DATASET.dataset_id, batch.events[0].event_id, 100)
        assert row.scheduled == value and row.scheduled.timestamp_ns is None
        assert row.timing_reference_ids
        assert repo.event(row.artifact_id, deep=True) == row
