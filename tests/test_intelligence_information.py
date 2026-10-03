"""Existing EIE-5 contracts remain separately sourced and causally bounded."""

from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import NumericRange
from eie.expectations.consensus import consensus_snapshot
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import ObservationSource
from eie.expectations.observations import ActualObservation, ActualRole
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.surprise import calculate_surprise
from eie.expectations.units import Quantity
from eie.intelligence.repository import IntelligenceRepository
from eie.persistence.sqlite import SQLiteEventStore
from eie.sources.trust import SourcePrecedencePolicy
from examples.intelligence_fixtures import ISSUER, USD, batch, source
from tests.intelligence_helpers import compute, ingest, metrics, novelty


def metadata(provider, item):
    raw = item.raw
    return ObservationSource(
        provider.source.name,
        raw.source_item_id,
        raw.observation_id,
        provider.source.source_class,
        item.lineage.known_from_ns,
        raw.source_observed_at_ns,
        raw.ingested_at_ns,
    )


def test_sourced_guidance_history_surprise_separation_and_rejection():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        old = batch(
            p,
            10,
            NumericRange(Quantity(10, USD), Quantity(20, USD)),
            measurement="GUIDANCE.REVENUE",
            key="old",
        )
        new = batch(
            p,
            20,
            NumericRange(Quantity(15, USD), Quantity(25, USD)),
            measurement="GUIDANCE.REVENUE",
            key="new",
        )
        for item in (old, new):
            ingest(store, p, item)
        repo = IntelligenceRepository(store)
        guidance = GuidanceObservation(
            old.claims[0].claim.measurement,
            ISSUER,
            metadata(p, old),
            old.events[0].revision,
            10,
            low=Quantity(10, USD),
            high=Quantity(20, USD),
        )
        identity = repo.append_guidance(guidance)
        assert repo.append_guidance(guidance) == identity
        result = compute(store, new, guidance_ids=(identity,))
        assert metrics(novelty(result, "GUIDANCE")[0])["midpoint_change"] == 5
        for value in (
            replace(guidance, low=Quantity(9, USD)),
            replace(guidance, source=replace(guidance.source, provider_id="ABSENT")),
            replace(guidance, context=replace(guidance.context, event_id="evt_" + "f" * 64)),
        ):
            with pytest.raises(ValidationError):
                repo.append_guidance(value)
        with pytest.raises(PersistenceError):
            compute(store, new, guidance_ids=("absent",))
        future = batch(p, 30, Quantity(40, USD), measurement="GUIDANCE.REVENUE", key="future")
        ingest(store, p, future)
        future_guidance = GuidanceObservation(
            future.claims[0].claim.measurement,
            ISSUER,
            metadata(p, future),
            future.events[0].revision,
            30,
            point=Quantity(40, USD),
        )
        future_id = repo.append_guidance(future_guidance)
        with pytest.raises(ValidationError):
            compute(store, new, guidance_ids=(future_id,))
        store._connection.execute("DROP TRIGGER immutable_ei_information_update")
        store._connection.execute("UPDATE ei_information SET known=19 WHERE id=?", (future_id,))
        with pytest.raises(PersistenceError):
            compute(store, new, guidance_ids=(future_id,))
        store._connection.execute(
            "UPDATE ei_information SET fingerprint='corrupt' WHERE id=?", (identity,)
        )
        with pytest.raises(PersistenceError):
            compute(store, new, guidance_ids=(identity,))


def test_surprise_recalculation_cannot_validate_unsupported_actual_values():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        item = batch(p, 20, Quantity(100, USD))
        ingest(store, p, item)
        context = item.claims[0].claim.measurement
        actual = ActualObservation(
            context,
            metadata(p, item),
            Quantity(100, USD),
            ActualRole.ACTUAL,
            20,
            item.events[0].revision,
        )
        settings = ExpectationsSettings()
        fp = fingerprint("eie:information-test:v1", settings)
        consensus = consensus_snapshot(context, (), 19, settings, SourcePrecedencePolicy(), fp)
        good = calculate_surprise(context, 20, actual, consensus, settings, fp)
        repo = IntelligenceRepository(store)
        identity = repo.append_surprise(good)
        result = compute(store, item, surprise_ids=(identity,))
        assert result.materiality == compute(store, item).materiality
        forged = calculate_surprise(
            context, 20, replace(actual, value=Quantity(999, USD)), consensus, settings, fp
        )
        with pytest.raises(ValidationError):
            repo.append_surprise(forged)
        with pytest.raises(ValidationError):
            repo.append_surprise(replace(good, raw_surprise=999))
        other = batch(p, 30, key="other")
        ingest(store, p, other)
        with pytest.raises(ValidationError):
            compute(store, other, surprise_ids=(identity,))
        with pytest.raises(PersistenceError):
            compute(store, item, surprise_ids=("absent",))
