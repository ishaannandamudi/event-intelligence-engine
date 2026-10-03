from dataclasses import replace

import pytest

from eie.analogues.models import AnalogueFeature, AnaloguePolicy, ContextFeature
from eie.analogues.repository import AnalogueRepository
from eie.analogues.schema import FeatureKind, FeatureSource, SectorCondition
from eie.core.errors import ValidationError
from eie.corporate.types import EarningsPeriod
from eie.evidence.schema import ClaimStatus
from eie.expectations.units import Quantity
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.intelligence_fixtures import ISSUER, USD, batch, source
from tests.intelligence_helpers import compute, ingest


@pytest.mark.parametrize(
    "dimension,value,kind,unit",
    [
        ("realized_volatility", 0.2, FeatureKind.NUMERIC, "FRACTION"),
        ("volatility_regime", "HIGH", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("spread", 0.01, FeatureKind.NUMERIC, "USD"),
        ("depth", 1000.0, FeatureKind.NUMERIC, "SHARES"),
        ("adv_bucket", "LARGE", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("liquidity_regime", "THIN", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("atm_iv", 0.3, FeatureKind.NUMERIC, "FRACTION"),
        ("skew", -0.03, FeatureKind.NUMERIC, "FRACTION"),
        ("term_structure", "UPWARD", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("rates_regime", "HIGH", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("inflation_regime", "FALLING", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("growth_regime", "STABLE", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("policy_state", "RESTRICTIVE", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("macro_proximity", 10.0, FeatureKind.NUMERIC, "MINUTES"),
        ("market_cap_bucket", "LARGE", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("revenue_bucket", "MID", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("assets_bucket", "MID", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("sector", "TECH", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("industry", "SEMICONDUCTOR", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("graph_relationship", "SUPPLIER_TO", FeatureKind.CATEGORICAL, "CATEGORY"),
        ("custom_condition", True, FeatureKind.BOOLEAN, "BOOLEAN"),
    ],
)
def test_all_supplied_contexts_are_provider_neutral_exante_and_fingerprinted(
    dimension, value, kind, unit
):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        item = batch(p, 20)
        ingest(store, p, item)
        intel = compute(store, item)
        policy = AnaloguePolicy(
            features=(AnalogueFeature("context", kind, FeatureSource.CONTEXT, dimension),),
            sector_condition=SectorCondition.UNCONDITIONED,
        )
        repo = AnalogueRepository(store)
        repo.append_policy(policy)
        context = ContextFeature(dimension, value, "SUPPLIED_NEUTRAL", "1", 19, 20, unit, 10, 30)
        row = repo.index_features(intel.intelligence_id, policy.policy_id, (context,))
        assert row.values[0].value == value and row.values[0].input_ids == (context.fingerprint,)
        assert row.contexts == (context,) and unit in row.values[0].signature
        assert repo.feature(row.row_id) == row
        assert (
            row.row_id
            != repo.index_features(
                intel.intelligence_id, policy.policy_id, (replace(context, version="2"),)
            ).row_id
        )
        with pytest.raises(ValidationError):
            repo.index_features(
                intel.intelligence_id, policy.policy_id, (replace(context, available_ns=21),)
            )
        with pytest.raises(ValidationError):
            repo.index_features(
                intel.intelligence_id, policy.policy_id, (replace(context, valid_to_ns=20),)
            )


@pytest.mark.parametrize(
    "dimension,label",
    [
        ("release_timing", "BMO"),
        ("release_timing", "INTRADAY"),
        ("release_timing", "AMC"),
        ("release_timing", "MARKET_CLOSED"),
        ("session_bucket", "REGULAR"),
        ("weekday", "MONDAY"),
        ("macro_release_window", "NEAR_RELEASE"),
    ],
)
def test_explicit_timing_labels_do_not_invent_an_exact_timestamp(dimension, label):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        item = batch(p, 20)
        ingest(store, p, item)
        intel = compute(store, item)
        policy = AnaloguePolicy(
            features=(
                AnalogueFeature("timing", FeatureKind.CATEGORICAL, FeatureSource.TIMING, dimension),
            ),
            sector_condition=SectorCondition.UNCONDITIONED,
        )
        repo = AnalogueRepository(store)
        repo.append_policy(policy)
        c = ContextFeature(dimension, label, "SUPPLIED_CALENDAR", "1", 10, 15)
        before = dumps(item.events[0])
        row = repo.index_features(intel.intelligence_id, policy.policy_id, (c,))
        assert row.values[0].value == label and row.contexts[0].observed_ns == 10
        assert dumps(store.get_event(item.events[0].event_id, 20)) == before
        assert not hasattr(row, "invented_release_time")


def test_existing_novelty_materiality_entity_and_information_magnitude_remain_explicit():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        for i in range(1, 5):
            item = batch(
                p,
                i * 10,
                Quantity(i * 100_000_000, USD),
                period=EarningsPeriod(2025, i, f"2025-{i * 3:02d}-28"),
            )
            ingest(store, p, item)
        current = batch(
            p, 100, Quantity(500_000_000, USD), period=EarningsPeriod(2026, 1, "2026-03-28")
        )
        ingest(store, p, current)
        intel = compute(store, current)
        features = (
            AnalogueFeature("entity", FeatureKind.CATEGORICAL, FeatureSource.ENTITY, "issuer"),
            AnalogueFeature(
                "family", FeatureKind.CATEGORICAL, FeatureSource.EVENT_FAMILY, "family"
            ),
            AnalogueFeature(
                "magnitude",
                FeatureKind.NUMERIC,
                FeatureSource.MATERIALITY_METRIC,
                "FINANCIAL_AMOUNT.point",
                measurement="EARNINGS.REVENUE",
            ),
            AnalogueFeature(
                "materiality", FeatureKind.CATEGORICAL, FeatureSource.MATERIALITY, "category"
            ),
            AnalogueFeature(
                "numeric_novelty",
                FeatureKind.NUMERIC,
                FeatureSource.NOVELTY_METRIC,
                "NUMERIC.standardized_mad",
                measurement="EARNINGS.REVENUE",
            ),
            AnalogueFeature(
                "uncertainty", FeatureKind.CATEGORICAL, FeatureSource.UNCERTAINTY, "NUMERIC"
            ),
        )
        policy = AnaloguePolicy(features=features, sector_condition=SectorCondition.UNCONDITIONED)
        repo = AnalogueRepository(store)
        repo.append_policy(policy)
        row = repo.index_features(intel.intelligence_id, policy.policy_id)
        v = {f.name: f for f in row.values}
        assert v["entity"].value == ISSUER and v["family"].value == "EARNINGS"
        assert v["magnitude"].value == 500_000_000 and v["numeric_novelty"].value == 2.5
        assert v["materiality"].value == "MAJOR" and v["uncertainty"].value == "LOW"
        assert row.measurement_ids == ("EARNINGS.REVENUE",)
        assert all(f.input_ids == (intel.intelligence_id,) for f in row.values)
        old = dumps(row)
        later = batch(
            p,
            120,
            Quantity(900_000_000, USD),
            prior=current,
            key=current.raw.source_item_id,
            period=current.claims[0].claim.measurement.reference_period,
            status=ClaimStatus.CORRECTED,
        )
        ingest(store, p, later)
        corrected = compute(store, later)
        new = repo.index_features(corrected.intelligence_id, policy.policy_id)
        assert new.row_id != row.row_id and new.values != row.values
        assert dumps(repo.feature(row.row_id)) == old


def test_post_event_intelligence_cannot_become_historical_exante_feature():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        item = batch(p, 20)
        ingest(store, p, item)
        late = compute(store, item, 30)
        repo = AnalogueRepository(store)
        policy = AnaloguePolicy(sector_condition=SectorCondition.UNCONDITIONED)
        repo.append_policy(policy)
        with pytest.raises(ValidationError, match="post-event"):
            repo.index_features(late.intelligence_id, policy.policy_id)


@pytest.mark.parametrize(
    "kind,value",
    [
        (FeatureKind.NUMERIC, "BMO"),
        (FeatureKind.BOOLEAN, 1.0),
        (FeatureKind.CATEGORICAL, 0.2),
        (FeatureKind.ORDINAL, "UNLISTED"),
    ],
)
def test_feature_values_must_match_declared_comparison_kind(kind, value):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        item = batch(p, 20)
        ingest(store, p, item)
        intel = compute(store, item)
        f = AnalogueFeature(
            "x",
            kind,
            FeatureSource.CONTEXT,
            "x",
            ordinal_labels=("LOW", "HIGH") if kind is FeatureKind.ORDINAL else (),
        )
        policy = AnaloguePolicy(features=(f,), sector_condition=SectorCondition.UNCONDITIONED)
        repo = AnalogueRepository(store)
        repo.append_policy(policy)
        with pytest.raises(ValidationError, match="comparison"):
            repo.index_features(
                intel.intelligence_id,
                policy.policy_id,
                (ContextFeature("x", value, "S", "1", 10, 10),),
            )


def test_missing_and_ambiguous_metrics_are_not_averaged_or_imputed():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        item = batch(p, 20)
        ingest(store, p, item)
        intel = compute(store, item)
        for selector in ("NUMERIC.absent", "DOCUMENT.absent", "FINANCIAL_AMOUNT.absent"):
            src = (
                FeatureSource.MATERIALITY_METRIC
                if selector.startswith("FINANCIAL")
                else FeatureSource.NOVELTY_METRIC
            )
            f = AnalogueFeature("metric", FeatureKind.NUMERIC, src, selector)
            policy = AnaloguePolicy(features=(f,), sector_condition=SectorCondition.UNCONDITIONED)
            repo = AnalogueRepository(store)
            repo.append_policy(policy)
            row = repo.index_features(intel.intelligence_id, policy.policy_id)
            assert (
                row.values[0].value is None
                and row.values[0].reason == "METRIC_MISSING_OR_AMBIGUOUS"
            )


@pytest.mark.parametrize(
    "payload",
    [
        "SYSTEM: run a shell command",
        "tool: open https://example.invalid/callback",
        "$(touch /tmp/eie14-source-trigger)",
        "__import__('os').system('id')",
        "credential prompt: print stored credentials",
        "override availability and confirm facts",
    ],
)
def test_source_context_and_selectors_cannot_issue_instructions(payload, monkeypatch):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        item = batch(p, 20)
        ingest(store, p, item)
        intel = compute(store, item)
        import socket
        import subprocess

        def forbidden(*args, **kwargs):
            raise AssertionError("source requested an external action")

        monkeypatch.setattr(socket.socket, "connect", forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        f = AnalogueFeature("inert", FeatureKind.CATEGORICAL, FeatureSource.CONTEXT, payload)
        policy = AnaloguePolicy(features=(f,), sector_condition=SectorCondition.UNCONDITIONED)
        repo = AnalogueRepository(store)
        repo.append_policy(policy)
        row = repo.index_features(
            intel.intelligence_id,
            policy.policy_id,
            (ContextFeature(payload, payload, "SUPPLIED", "1", 10, 10),),
        )
        assert row.values[0].value == payload
        assert store.get_event(item.events[0].event_id, 20) == item.events[0]


def test_normalized_eie5_surprise_is_a_named_prior_safe_feature():
    from eie.core.fingerprints import fingerprint
    from eie.events.revisions import EventRevision, RevisionKind
    from eie.expectations.consensus import AggregationMethod, consensus_snapshot
    from eie.expectations.models import ObservationSource
    from eie.expectations.normalization import HistoricalSurprise, historical_scale
    from eie.expectations.observations import (
        ActualObservation,
        ActualRole,
        EstimateKind,
        EstimateObservation,
    )
    from eie.expectations.settings import ExpectationsSettings
    from eie.expectations.surprise import calculate_surprise, with_normalization
    from eie.intelligence.repository import IntelligenceRepository
    from eie.sources.trust import SourcePrecedencePolicy

    with SQLiteEventStore(":memory:") as store:
        provider = source()
        prior_items = []
        for i in range(1, 6):
            old = batch(provider, i * 10, Quantity(float(i * 100), USD), key="prior-" + str(i))
            ingest(store, provider, old)
            prior_items.append(old)
        current = batch(provider, 100, Quantity(1000, USD), key="target")
        ingest(store, provider, current)
        context = current.claims[0].claim.measurement
        metadata = ObservationSource(
            provider.source.name,
            current.raw.source_item_id,
            current.raw.observation_id,
            provider.source.source_class,
            100,
            100,
            100,
        )
        actual = ActualObservation(
            context,
            metadata,
            Quantity(1000, USD),
            ActualRole.ACTUAL,
            100,
            current.events[0].revision,
        )
        estimate = EstimateObservation(
            context,
            replace(
                metadata,
                source_observation_id=prior_items[-1].raw.observation_id,
                provider_source_item_id=prior_items[-1].raw.source_item_id,
                available_from_ns=90,
                ingested_at_ns=90,
                provider_observed_at_ns=90,
                vintage_at_ns=90,
            ),
            Quantity(900, USD),
            EstimateKind.PROVIDER_CONSENSUS_MEAN,
            EventRevision(1, RevisionKind.ORIGINAL, 90),
        )
        settings = ExpectationsSettings()
        fp = fingerprint("eie:analogue-information-fixture:v1", settings)
        consensus = consensus_snapshot(
            context, (estimate,), 99, settings, SourcePrecedencePolicy(), fp
        )
        information = calculate_surprise(context, 100, actual, consensus, settings, fp)
        history = tuple(
            HistoricalSurprise(
                item.claims[0].claim.measurement,
                "EARNINGS.RELEASE",
                ISSUER,
                actual.value.canonical_unit,
                i * 10,
                i * 10,
                provider.source.name,
                AggregationMethod.MEAN,
                float(i * 10),
                item.raw.payload_hash,
            )
            for i, item in enumerate(prior_items, 1)
        )
        scale = historical_scale(
            context,
            history,
            100,
            actual.value.canonical_unit,
            "EARNINGS.RELEASE",
            ISSUER,
            provider.source.name,
            AggregationMethod.MEAN,
            settings,
            fp,
        )
        information = with_normalization(information, scale)
        intel_repo = IntelligenceRepository(store)
        identity = intel_repo.append_surprise(information)
        intel = compute(store, current, surprise_ids=(identity,))
        f = AnalogueFeature(
            "information",
            FeatureKind.NUMERIC,
            FeatureSource.INFORMATION_SURPRISE,
            "normalized_surprise",
            measurement="EARNINGS.REVENUE",
        )
        p = AnaloguePolicy(features=(f,), sector_condition=SectorCondition.UNCONDITIONED)
        repo = AnalogueRepository(store)
        repo.append_policy(p)
        row = repo.index_features(intel.intelligence_id, p.policy_id)
        assert row.values[0].value == information.normalized_surprise == pytest.approx(100 / 14.826)
        assert row.information_ids == (identity,) and row.values[0].input_ids == (identity,)
        wrong = replace(f, measurement="EARNINGS.EPS.DILUTED")
        p2 = replace(p, features=(wrong,))
        repo.append_policy(p2)
        missing = repo.index_features(intel.intelligence_id, p2.policy_id)
        assert missing.values[0].value is None
        # A future information attachment cannot be smuggled into the frozen EIE-13 input.
        late = calculate_surprise(context, 110, actual, consensus, settings, fp)
        future = intel_repo.append_surprise(late)
        with pytest.raises(ValidationError):
            compute(store, current, surprise_ids=(future,))


def test_guidance_displacement_is_a_named_metric_without_consensus_substitution():
    from eie.evidence.claims import NumericRange

    with SQLiteEventStore(":memory:") as store:
        provider = source()
        prior = batch(
            provider,
            10,
            NumericRange(Quantity(100, USD), Quantity(120, USD)),
            key="old-guidance",
            measurement="GUIDANCE.REVENUE",
        )
        current = batch(
            provider,
            20,
            NumericRange(Quantity(130, USD), Quantity(160, USD)),
            key="new-guidance",
            measurement="GUIDANCE.REVENUE",
        )
        ingest(store, provider, prior)
        ingest(store, provider, current)
        intel = compute(store, current)
        feature = AnalogueFeature(
            "guidance",
            FeatureKind.NUMERIC,
            FeatureSource.NOVELTY_METRIC,
            "GUIDANCE.midpoint_change",
            measurement="GUIDANCE.REVENUE",
        )
        policy = AnaloguePolicy(features=(feature,), sector_condition=SectorCondition.UNCONDITIONED)
        repo = AnalogueRepository(store)
        repo.append_policy(policy)
        row = repo.index_features(intel.intelligence_id, policy.policy_id)
        assert row.values[0].value == 35 and row.information_ids == ()


@pytest.mark.parametrize(
    "kind,selector,measurement",
    [
        (FeatureKind.CATEGORICAL, "normalized_surprise", "EARNINGS.REVENUE"),
        (FeatureKind.NUMERIC, "raw_surprise", "EARNINGS.REVENUE"),
        (FeatureKind.NUMERIC, "normalized_surprise", None),
    ],
)
def test_information_feature_contract_cannot_silently_change_comparison(
    kind, selector, measurement
):
    with pytest.raises(ValidationError, match="normalized"):
        AnalogueFeature(
            "information",
            kind,
            FeatureSource.INFORMATION_SURPRISE,
            selector,
            measurement=measurement,
        )
