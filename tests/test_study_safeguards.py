from dataclasses import replace

import pytest

from eie.backfill.models import ReconstructionQuality
from eie.core.errors import PersistenceError, ValidationError
from eie.persistence import codec
from eie.studies.decay import EventDecayResult, InfluencePoint
from eie.studies.engine import StudyEngine, calculate
from eie.studies.liquidity import bucket, quote_response, volume_response
from eie.studies.market import (
    Adjustment,
    IndexedMarketProvider,
    MarketQuote,
    MarketSession,
    OptionsMarketObservation,
    Timeframe,
)
from eie.studies.models import (
    AnchorSemantics,
    BenchmarkInput,
    BenchmarkType,
    ContaminationFlag,
    ContextLabel,
    ResponsePoint,
    StudyPolicy,
    StudyQuality,
    StudyRunManifest,
    VolatilityResponse,
    VolumePoint,
)
from eie.studies.options import change, options_response
from eie.studies.repository import StudyRepository
from eie.studies.validation import StudyValidationStatus, validate_study
from examples.event_studies import DAY, MINUTE, history, market
from tests.study_helpers import inputs_history, result, window
from tests.test_study_history import record


@pytest.fixture
def sample(tmp_path):
    store, reader, ids = history(tmp_path)
    yield store, reader, result(reader, ids[-1])
    store.close()


@pytest.mark.parametrize(
    "target,changes",
    [
        ("definition", {"schema_version": "2.0"}),
        ("definition", {"historical_fact_id": "bad"}),
        ("definition", {"historical_fact_id": "fact_bad"}),
        ("definition", {"event_gap_fingerprints": ("b" * 64, "a" * 64)}),
        ("definition", {"selection_as_of_ns": 0}),
        ("definition", {"data_as_of_ns": 0}),
        ("definition", {"window": replace(window(), anchor_semantics=AnchorSemantics.SCHEDULED)}),
        ("definition", {"policy": StudyPolicy(required_quality=ReconstructionQuality.UNKNOWN)}),
        (
            "definition",
            {
                "context": (
                    ContextLabel("x", "a", "S", "1", 0, 0),
                    ContextLabel("x", "b", "S", "1", 0, 0),
                )
            },
        ),
        ("result", {"available_ns": 0}),
        ("result", {"anchor_ns": 0}),
        ("result", {"curve": ()}),
        (
            "result",
            {
                "market": replace(
                    market(),
                    identity=replace(market().identity, timeframe=Timeframe.M5),
                    bars=tuple(replace(b, timeframe=Timeframe.M5) for b in market().bars),
                )
            },
        ),
        (
            "result",
            {
                "benchmarks": (
                    BenchmarkInput(BenchmarkType.MARKET_INDEX, "x", None),
                    BenchmarkInput(BenchmarkType.MARKET_INDEX, "x", None),
                )
            },
        ),
    ],
)
def test_completed_contract_validation(sample, target, changes):
    _, _, r = sample
    with pytest.raises(ValidationError):
        replace(r.definition if target == "definition" else r, **changes)


def test_small_output_contract_guards():
    for builder in [
        lambda: ResponsePoint(1, 1, None, 1.0),
        lambda: ResponsePoint(1, 1, 1, None),
        lambda: VolatilityResponse(-1, None, None, None, 0, 0),
        lambda: VolumePoint(0, -1, 1, None, None, 0),
        lambda: ContaminationFlag("e", "r", 0, "S", ("B", "A"), 0),
        lambda: BenchmarkInput(BenchmarkType.MARKET_INDEX, "other", market()),
        lambda: InfluencePoint(0, -1, 1),
        lambda: InfluencePoint(0, 1, -1),
        lambda: EventDecayResult(
            "BAD", (), (), None, None, 0.5, None, "FIRST_SAMPLED_FRACTION_OF_PEAK", ()
        ),
        lambda: EventDecayResult(
            "SINGLE_EVENT",
            (),
            (InfluencePoint(1, 1, 1), InfluencePoint(0, 1, 1)),
            None,
            None,
            0.5,
            None,
            "FIRST_SAMPLED_FRACTION_OF_PEAK",
            (),
        ),
        lambda: EventDecayResult(
            "EVENT_CLASS", (), (), None, None, 0.5, None, "FIRST_SAMPLED_FRACTION_OF_PEAK", ()
        ),
        lambda: StudyRunManifest("b", "a" * 64, ("b", "a"), ("a" * 64, "b" * 64), 0, 0),
    ]:
        with pytest.raises(ValidationError):
            builder()


def test_liquidity_baseline_and_numeric_overflow_guards():
    bars = market().bars[:6]
    daily = replace(bars[0], timeframe=Timeframe.DAILY)
    assert bucket(daily, ()) == ("DAILY", 0)
    assert bucket(bars[0], ()) is None
    sessions = (MarketSession("s", bars[0].start_ns, bars[-1].end_ns, 0, "S", "1"),)
    points = volume_response(
        bars[:2], bars[2:3], bars, sessions, bars[2].start_ns, StudyPolicy(minimum_baseline_bars=1)
    )
    assert all(p.baseline is None for p in points)  # intraday buckets never mix
    zeros = tuple(replace(b, volume=0, timeframe=Timeframe.DAILY) for b in bars)
    p = volume_response(
        zeros[3:4], zeros[4:5], zeros, (), zeros[4].start_ns, StudyPolicy(minimum_baseline_bars=1)
    )
    assert p[0].baseline == 0 and p[0].relative is None
    huge = tuple(replace(b, volume=1e308, timeframe=Timeframe.DAILY) for b in bars)
    assert volume_response(huge[:3], (), huge, (), huge[-1].end_ns, StudyPolicy()) == ()
    quotes = (
        MarketQuote("s", 0, 0, 0, 0, 0, 0, "S"),
        MarketQuote("s", 1, 1, 0, 0, 1e308, 1e308, "S"),
    )
    q = quote_response(quotes, 0, 0, 1, 1)
    assert q.quoted_depth is None and q.relative_spread is None and q.midpoint_return is None
    assert change(None, 1) is None and change(1e308, -1e308) is None


def test_missing_options_fields_do_not_become_zero():
    values = tuple(
        OptionsMarketObservation("s", t, t, "2026-12-01", "1M", "ATM", 0.2, 0, None, None, "S")
        for t in (0, 1)
    )
    r = options_response(values, ("2026-12-01", "1M", "ATM"), 0, 0, 1, 1)
    assert r.iv_change == 0 and r.volume_change is None and r.open_interest_change is None


def test_future_conditioning_benchmark_clocks_and_invalid_contamination(sample):
    _, _, r = sample
    d = r.definition
    late = ContextLabel("regime", "stable", "S", "1", r.anchor_ns + 1, r.anchor_ns + 1)
    with pytest.raises(ValidationError):
        calculate(replace(d, context=(late,)), r.market, (), r.contamination)
    flag = replace(r.contamination[0], available_ns=d.selection_as_of_ns + 1)
    with pytest.raises(ValidationError):
        calculate(d, r.market, (), (flag,))
    wrong = replace(r.contamination[0], event_id=d.event.event_id)
    with pytest.raises(ValidationError):
        calculate(d, r.market, (), (wrong,))
    assert (
        validate_study(
            replace(
                r, definition=replace(d, anchor=replace(d.anchor, timestamp_ns=r.anchor_ns + 1))
            )
        ).status
        is StudyValidationStatus.INVALID
    )


def test_stored_run_damage_and_result_size_bound(sample, monkeypatch):
    store, reader, r = sample
    repo = StudyRepository(store)
    monkeypatch.setattr(codec, "dumps", lambda value: b"X" * 20_000_001)
    with pytest.raises(ValidationError):
        repo.append(r)
    monkeypatch.undo()
    run = repo.append_run((r,))
    store._connection.execute("DROP TRIGGER immutable_study_runs_update")
    # Typed manifest with altered content is rejected independently of scalar indexes.
    altered = replace(run, contamination_count=0)
    store._connection.execute("UPDATE study_runs SET payload=?", (codec.dumps(altered),))
    with pytest.raises(PersistenceError):
        repo.run(run.run_id)


def test_unknown_event_time_and_known_split_propagate(tmp_path):
    at = 9 * DAY + 10 * MINUTE
    main = record(at)
    unknown = record(at - 1, "unknown")
    unknown = replace(
        unknown, record=replace(unknown.record, published_at_ns=None, effective_at_ns=None)
    )
    split = record(at - 1, "split")
    from eie.events.taxonomy import EventFamily, EventType

    split = replace(
        split,
        record=replace(
            split.record,
            identity=replace(
                split.record.identity,
                event_type=EventType(EventFamily.CORPORATE_ACTION, "CORPORATE_ACTION.STOCK_SPLIT"),
            ),
            effective_at_ns=at + MINUTE,
        ),
    )
    store, reader = inputs_history(tmp_path, (main, unknown, split))
    try:
        data = replace(
            market(), identity=replace(market().identity, adjustment=Adjustment.UNADJUSTED)
        )
        engine = StudyEngine(reader, IndexedMarketProvider((data,)), at + 5 * MINUTE)
        r = engine.study_event(
            main.record.event_id,
            "security_fixture",
            engine.anchor(main.record.event_id, AnchorSemantics.PUBLICATION),
            window(),
            data_as_of_ns=at + 5 * MINUTE,
        )
        assert StudyQuality.EVENT_HISTORY_INCOMPLETE in r.quality
        assert StudyQuality.CORPORATE_ACTION_RISK in r.quality and r.post_return is None
        assert r.market.splits[0].source == "EIE_HISTORY"
    finally:
        store.close()


def test_input_cap_and_partial_endpoints_are_explicit(sample):
    _, reader, r = sample
    with pytest.raises(ValidationError):
        calculate(
            replace(
                r.definition, policy=replace(r.definition.policy, maximum_input_observations=1)
            ),
            r.market,
            r.benchmarks,
            r.contamination,
        )
    with pytest.raises(ValidationError):
        StudyPolicy(maximum_input_observations=100_001)
    trimmed = replace(r.market, bars=tuple(b for b in r.market.bars if b.end_ns != r.post_end_ns))
    missing = calculate(r.definition, trimmed, (), r.contamination)
    assert missing.post_return is None and StudyQuality.DATA_GAP in missing.quality


def test_historical_fact_validation_with_original_build(sample):
    _, reader, r = sample
    altered = replace(r, definition=replace(r.definition, historical_fact_id="fact_" + "f" * 64))
    assert "HISTORICAL_FACT_REFERENCE_MISMATCH" in validate_study(altered, reader).reasons


@pytest.mark.parametrize("instrument", ["security_fixture", "index_fixture"])
def test_provider_identity_drift_fails_closed(sample, instrument):
    _, reader, r = sample

    class Changed(IndexedMarketProvider):
        def read(self, symbol, start, end):
            data = super().read(symbol, start, end)
            return (
                replace(data, identity=replace(data.identity, version="changed"))
                if symbol == instrument
                else data
            )

    provider = Changed((market(), market(instrument="index_fixture")))
    engine = StudyEngine(reader, provider, r.definition.selection_as_of_ns)
    with pytest.raises(ValidationError):
        engine.study_event(
            r.definition.event.event_id,
            "security_fixture",
            r.definition.anchor,
            r.definition.window,
            data_as_of_ns=r.definition.data_as_of_ns,
            benchmarks=((BenchmarkType.MARKET_INDEX, "index_fixture"),),
        )
