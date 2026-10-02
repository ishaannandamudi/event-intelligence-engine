"""Deterministic offline model comparison, retained raw study, and restart validation."""

from dataclasses import replace
from hashlib import sha256
from math import sin
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.core.serialization import canonical_json_bytes
from eie.impact.engine import ImpactEngine
from eie.impact.estimation import period_returns, semantics
from eie.impact.inputs import (
    ControlCandidate,
    FactorObservation,
    FactorSeries,
    FeatureSnapshot,
    ImpactInputs,
)
from eie.impact.models import CARWindow, EstimationWindow, MatchingPolicy, ModelType
from eie.impact.repository import ImpactRepository
from eie.impact.sensitivity import contamination_sensitivity, impact_set
from eie.impact.validation import ImpactValidationStatus, validate_impact
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.studies.engine import StudyEngine
from eie.studies.market import (
    Adjustment,
    IndexedMarketProvider,
    MarketBar,
    MarketSeriesIdentity,
    MarketSession,
    MarketSlice,
    ReturnType,
    Timeframe,
)
from eie.studies.models import (
    AnchorSemantics,
    EventWindow,
    ReturnMethod,
    SessionPolicy,
    StudyPolicy,
)
from examples.event_studies import DAY, MINUTE, history

ESTIMATION = EstimationWindow(-250 * MINUTE, -20 * MINUTE, 16 * MINUTE, SessionPolicy.ELAPSED)
CARS = (
    CARWindow("post", 0, 5 * MINUTE),
    CARWindow("short", 0, MINUTE),
    CARWindow("symmetric", -2 * MINUTE, 2 * MINUTE),
)


def supplied_market(
    anchor: int, instrument: str = "security_fixture", *, target: bool = True
) -> MarketSlice:
    bars = []
    close = 100.0
    for j in range(330):
        end = anchor + (j - 300) * MINUTE
        market_return = 0.001 * sin(j * 0.71) + 0.0003 * sin(j * 0.19)
        value = (
            (
                0.00005
                + 1.5 * market_return
                + 0.00005 * sin(j * 1.29)
                + (0.002 if 0 < j - 300 <= 5 else 0)
            )
            if target
            else market_return
        )
        opening, close = close, close * (1 + value)
        bars.append(
            MarketBar(
                instrument,
                end - MINUTE,
                end,
                opening,
                max(opening, close),
                min(opening, close),
                close,
                100.0,
                end,
                "SYNTHETIC",
                Timeframe.M1,
            )
        )
    identity = MarketSeriesIdentity(
        instrument,
        "GENERIC",
        "SYNTHETIC",
        "offline-" + instrument,
        sha256(canonical_json_bytes(tuple(bars))).hexdigest(),
        "1",
        Timeframe.M1,
        Adjustment.SPLIT_ADJUSTED,
        ReturnType.PRICE_RETURN,
    )
    session = MarketSession(
        "supplied-continuous", bars[0].start_ns, bars[-1].end_ns, 0, "SYNTHETIC", "calendar-1"
    )
    return MarketSlice(identity, tuple(bars), sessions=(session,))


def feature(instrument: str, anchor: int, size: float) -> FeatureSnapshot:
    values = (("beta", size / 10), ("size", size))
    return FeatureSnapshot(
        instrument,
        anchor - 30 * MINUTE,
        anchor - 30 * MINUTE,
        values,
        "SYNTHETIC",
        "features-" + instrument,
        sha256(canonical_json_bytes(values)).hexdigest(),
        "1",
    )


def fixture(reader, event_id: str, *, method: ReturnMethod = ReturnMethod.SIMPLE):
    anchor = 9 * DAY + 10 * MINUTE
    target = supplied_market(anchor)
    benchmark = supplied_market(anchor, "market_fixture", target=False)
    sector = supplied_market(anchor, "sector_fixture", target=False)
    provider = IndexedMarketProvider((target,))
    engine = StudyEngine(reader, provider, anchor + 5 * MINUTE)
    window = EventWindow(
        AnchorSemantics.PUBLICATION,
        4 * MINUTE,
        5 * MINUTE,
        Timeframe.M1,
        SessionPolicy.ELAPSED,
        tuple(j * MINUTE for j in range(-4, 6)),
    )
    study = engine.study_event(
        event_id,
        target.identity.instrument_id,
        engine.anchor(event_id, AnchorSemantics.PUBLICATION),
        window,
        data_as_of_ns=anchor + 5 * MINUTE,
        policy=StudyPolicy(return_method=method, contamination_post_ns=5 * MINUTE),
    )
    base = ImpactInputs(study, target, benchmark)
    control_inputs = ImpactInputs(
        study,
        target,
        target_features=feature(target.identity.instrument_id, anchor, 10),
        controls=tuple(
            ControlCandidate(i, supplied_market(anchor, i, target=False), feature(i, anchor, size))
            for i, size in (("control_a", 9), ("control_b", 11), ("control_c", 20))
        ),
    )
    return base, replace(base, benchmark=sector), control_inputs


def factor_inputs(base: ImpactInputs) -> ImpactInputs:
    assert base.benchmark is not None
    meaning = semantics(base, ESTIMATION)
    observations = tuple(
        FactorObservation(p.start_ns, p.end_ns, p.value, p.available_ns)
        for p in period_returns(base.benchmark, meaning, base.study.definition.data_as_of_ns)
    )
    factors = (
        FactorSeries(
            "market",
            "SYNTHETIC",
            "factor-market",
            sha256(canonical_json_bytes(observations)).hexdigest(),
            "1",
            meaning,
            observations,
        ),
    )
    return replace(base, benchmark=None, factors=factors)


def demo(root: Path) -> dict[str, object]:
    store, reader, ids = history(root)
    try:
        base, sector, controls = fixture(reader, ids[-1])
        engine = ImpactEngine()
        market = engine.impact_event(base, ModelType.MARKET_MODEL, ESTIMATION, CARS)
        adjusted = engine.impact_event(sector, ModelType.SECTOR_ADJUSTED, ESTIMATION, CARS)
        matched = engine.impact_event(
            controls, ModelType.MATCHED_CONTROLS, ESTIMATION, CARS, matching=MatchingPolicy(k=2)
        )
        factors = engine.impact_event(
            factor_inputs(base), ModelType.SUPPLIED_FACTOR_MODEL, ESTIMATION, CARS
        )
        for r in (market, adjusted, matched, factors):
            assert validate_impact(r, reader).status is not ImpactValidationStatus.INVALID
            assert codec.dumps(codec.loads(codec.dumps(r))) == codec.dumps(r)
        comparison = impact_set((market, adjusted, matched, factors))
        sensitivity = contamination_sensitivity(market)
        repository = ImpactRepository(store)
        run = repository.append_run(comparison.results)
        assert repository.append_set(comparison)
        assert repository.append_set(sensitivity)
        payload = codec.dumps(market)
        impact_id = market.impact_id
    finally:
        store.close()
    with SQLiteEventStore(root / "studies.sqlite") as restarted:
        loaded = ImpactRepository(restarted).get(impact_id)
        assert loaded is not None and codec.dumps(loaded) == payload
    return {
        "models": {
            r.model_spec.model_type.value: {
                "fit_status": r.fit.status.value,
                "observations": r.fit.observation_count,
                "CAR_post": r.cars[0].value,
                "CAR_uncertainty": None
                if r.cars[0].uncertainty is None
                else (r.cars[0].uncertainty.lower, r.cars[0].uncertainty.upper),
            }
            for r in comparison.results
        },
        "market_coefficients": market.fit.payload.coefficients,
        "model_comparisons": len(comparison.sensitivity),
        "contamination_variants": len(sensitivity.results),
        "result_fingerprint": market.result_fingerprint,
        "run_id": run.run_id,
        "restart_byte_identical": True,
        "interpretation": "Model-based event-window residuals; no causal proof or model winner.",
    }


if __name__ == "__main__":
    import json

    with TemporaryDirectory() as directory:
        print(json.dumps(demo(Path(directory)), sort_keys=True))
