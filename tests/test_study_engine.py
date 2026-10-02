from dataclasses import replace

import pytest

from eie.backfill.models import ReconstructionQuality
from eie.core.errors import ValidationError
from eie.persistence import codec
from eie.studies.engine import StudyEngine, calculate, run_manifest
from eie.studies.market import (
    Adjustment,
    IndexedMarketProvider,
    MarketGap,
    MarketQuote,
    OptionsMarketObservation,
    ReturnType,
    SplitBoundary,
)
from eie.studies.models import (
    AnchorQuality,
    AnchorSemantics,
    BenchmarkInput,
    BenchmarkType,
    ContextLabel,
    EventAnchor,
    ReferencePolicy,
    ReturnMethod,
    StudyPolicy,
    StudyQuality,
)
from eie.studies.validation import StudyValidationStatus, validate_study
from examples.event_studies import DAY, MINUTE, demo, history, market
from tests.study_helpers import result, window


@pytest.fixture
def historical(tmp_path):
    store, reader, ids = history(tmp_path)
    yield store, reader, ids
    store.close()


@pytest.mark.parametrize("method", list(ReturnMethod))
@pytest.mark.parametrize("reference", list(ReferencePolicy))
def test_end_to_end_reaction_and_repeatability(historical, method, reference):
    _, reader, ids = historical
    r = result(reader, ids[-1], policy=StudyPolicy(return_method=method, reference=reference))
    assert r.pre_return is not None and r.post_return is not None
    assert r.volatility.pre is not None and r.volatility.post is not None
    assert len(r.volume) == 9
    assert r.contamination
    assert all(v.end_ns <= r.anchor_ns for v in r.market.bars if v.end_ns <= r.anchor_ns)
    assert r == result(
        reader, ids[-1], policy=StudyPolicy(return_method=method, reference=reference)
    )
    assert codec.loads(codec.dumps(r)) == r
    assert validate_study(r, reader).status is StudyValidationStatus.VALID_WITH_WARNINGS
    assert r.definition.reconstruction_quality is ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME
    if reference is ReferencePolicy.ALIGNED_CLOSE:
        assert next(p for p in r.curve if p.offset_ns == 0).value is None


def test_future_bar_mutation_never_changes_pre_metrics(historical):
    _, reader, ids = historical
    data = market()
    at = 9 * DAY + 10 * MINUTE
    original = result(reader, ids[-1], data=data)
    altered = replace(
        data,
        bars=tuple(
            replace(
                b,
                open=b.open * 2,
                high=b.high * 2,
                low=b.low * 2,
                close=b.close * 2,
                volume=b.volume * 2,
            )
            if b.start_ns >= at
            else b
            for b in data.bars
        ),
    )
    changed = result(reader, ids[-1], data=altered)
    assert changed.pre_return == original.pre_return
    assert changed.volatility.pre == original.volatility.pre
    assert changed.reference_price == original.reference_price
    assert changed.post_return != original.post_return
    assert changed.volume[:4] == original.volume[:4]


@pytest.mark.parametrize(
    "change", ["missing_post", "missing_pre", "delayed_pre", "zero", "split", "gap"]
)
def test_quality_and_missing_data_are_explicit(historical, change):
    _, reader, ids = historical
    data = market()
    at = 9 * DAY + 10 * MINUTE
    if change == "missing_post":
        data = replace(data, bars=tuple(b for b in data.bars if b.start_ns < at))
    elif change == "missing_pre":
        data = replace(data, bars=tuple(b for b in data.bars if b.start_ns >= at))
    elif change == "delayed_pre":
        data = replace(
            data,
            bars=tuple(
                replace(b, available_ns=at + MINUTE) if b.end_ns <= at else b for b in data.bars
            ),
        )
    elif change == "zero":
        data = replace(
            data, bars=tuple(replace(b, open=0, low=0, high=0, close=0) for b in data.bars)
        )
    elif change == "split":
        data = replace(
            data,
            identity=replace(data.identity, adjustment=Adjustment.UNADJUSTED),
            splits=(SplitBoundary(at, at, "SYNTHETIC", "split"),),
        )
    else:
        data = replace(data, gaps=(MarketGap(at + MINUTE, at + 2 * MINUTE, "MISSING_INPUT"),))
    r = result(reader, ids[-1], data=data)
    expected = {
        "missing_post": StudyQuality.MISSING_POST_DATA,
        "missing_pre": StudyQuality.MISSING_PRE_DATA,
        "delayed_pre": StudyQuality.MISSING_PRE_DATA,
        "zero": StudyQuality.NUMERIC_UNAVAILABLE,
        "split": StudyQuality.CORPORATE_ACTION_RISK,
        "gap": StudyQuality.DATA_GAP,
    }[change]
    assert expected in r.quality
    if change in ("missing_post", "zero", "split"):
        assert r.post_return is None
    if change == "missing_post":
        assert all(p.value is None for p in r.curve if p.offset_ns > 0)
    if change == "gap":
        assert next(p for p in r.curve if p.offset_ns == 3 * MINUTE).value is None


def test_missing_internal_bar_is_not_flat_return(historical):
    _, reader, ids = historical
    data = market()
    end = 9 * DAY + 12 * MINUTE
    data = replace(data, bars=tuple(b for b in data.bars if b.end_ns != end))
    r = result(reader, ids[-1], data=data)
    assert next(p for p in r.curve if p.offset_ns == 2 * MINUTE).value is None
    assert StudyQuality.DATA_GAP in r.quality
    assert r.volatility.post_count == 2


def test_quote_and_options_reaction_causal_matching(historical):
    _, reader, ids = historical
    data = market()
    at = 9 * DAY + 10 * MINUTE
    quotes = (
        MarketQuote("security_fixture", at, at, 99, 101, 10, 20, "SYNTHETIC"),
        MarketQuote(
            "security_fixture", at + 4 * MINUTE, at + 4 * MINUTE, 100, 104, 20, 40, "SYNTHETIC"
        ),
    )
    options = tuple(
        OptionsMarketObservation(
            "security_fixture", t, t, "2026-12-01", "1M", "ATM", iv, skew, vol, oi, "SYNTHETIC"
        )
        for t, iv, skew, vol, oi in (
            (at, 0.2, -0.03, 100, 200),
            (at + 4 * MINUTE, 0.25, -0.02, 110, 220),
        )
    )
    r = result(
        reader,
        ids[-1],
        data=replace(data, quotes=quotes, options=options),
        policy=StudyPolicy(max_lag_ns=MINUTE, options_key=("2026-12-01", "1M", "ATM")),
    )
    assert r.liquidity.pre_spread == 2 and r.liquidity.post_spread == 4
    assert r.liquidity.quoted_depth == 60
    assert r.liquidity.relative_spread == pytest.approx(4 / 102)
    assert r.liquidity.midpoint_return == pytest.approx(0.02)
    assert r.options.iv_change == pytest.approx(0.05) and r.options.skew_change == pytest.approx(
        0.01
    )
    assert r.options.volume_change == 10 and r.options.open_interest_change == 20
    assert (
        StudyQuality.MISSING_QUOTES not in r.quality
        and StudyQuality.MISSING_OPTIONS not in r.quality
    )
    delayed = replace(
        data,
        quotes=(replace(quotes[0], available_ns=at + 1), quotes[1]),
        options=(replace(options[0], available_ns=at + 1), options[1]),
    )
    r = result(
        reader, ids[-1], data=delayed, policy=StudyPolicy(options_key=("2026-12-01", "1M", "ATM"))
    )
    assert r.liquidity.midpoint_return is None and r.options.iv_change is None


@pytest.mark.parametrize("kind", list(BenchmarkType))
def test_supplied_benchmark_and_absence(historical, kind):
    _, reader, ids = historical
    data = market()
    other = market(instrument="index_fixture")
    at = 9 * DAY + 10 * MINUTE
    engine = StudyEngine(reader, IndexedMarketProvider((data, other)), at + 5 * MINUTE)
    r = engine.study_event(
        ids[-1],
        "security_fixture",
        engine.anchor(ids[-1], AnchorSemantics.PUBLICATION),
        window(),
        data_as_of_ns=at + 5 * MINUTE,
        benchmarks=((kind, "index_fixture"),),
    )
    assert r.benchmark_responses[0].descriptive_difference == 0
    assert r.benchmarks[0].data.identity.instrument_id == "index_fixture"
    assert r.benchmark_responses[0].volatility.post is not None
    missing = result(reader, ids[-1], benchmarks=((kind, "absent"),))
    assert StudyQuality.MISSING_BENCHMARK in missing.quality
    assert missing.post_return is not None
    bad = replace(
        other,
        identity=replace(
            other.identity,
            adjustment=Adjustment.SPLIT_DIVIDEND_ADJUSTED,
            return_type=ReturnType.TOTAL_RETURN,
        ),
    )
    with pytest.raises(ValidationError):
        calculate(
            r.definition, r.market, (BenchmarkInput(kind, "index_fixture", bad),), r.contamination
        )


def test_identity_changes_dataset_build_policy_context(historical):
    _, reader, ids = historical
    r = result(reader, ids[-1])
    data = replace(market(), identity=replace(market().identity, dataset_fingerprint="b" * 64))
    assert result(reader, ids[-1], data=data).study_id != r.study_id
    assert (
        result(reader, ids[-1], policy=StudyPolicy(return_method=ReturnMethod.LOG)).study_id
        != r.study_id
    )
    assert (
        replace(r, definition=replace(r.definition, event_build_id="other")).study_id != r.study_id
    )
    context = (ContextLabel("regime", "stable", "SYNTHETIC", "1", 0, 0),)
    conditioned = result(reader, ids[-1], context=context)
    assert conditioned.study_id != r.study_id
    with pytest.raises(ValidationError):
        result(reader, ids[-1], context=(replace(context[0], available_ns=r.anchor_ns + 1),))


def test_anchor_selection_and_engine_errors(historical):
    _, reader, ids = historical
    at = 9 * DAY + 10 * MINUTE
    engine = StudyEngine(reader, IndexedMarketProvider((market(),)), at + 5 * MINUTE)
    assert engine.anchor(ids[-1], AnchorSemantics.SOURCE_AVAILABILITY).timestamp_ns == at
    assert engine.anchor(ids[-1], AnchorSemantics.EIE_AVAILABILITY).timestamp_ns == 20 * DAY
    for semantics in (AnchorSemantics.SCHEDULED, AnchorSemantics.ACCEPTANCE):
        with pytest.raises(ValidationError):
            engine.anchor(ids[-1], semantics)
    anchor = engine.anchor(ids[-1], AnchorSemantics.PUBLICATION)
    for event_id, instrument, chosen, cutoff in [
        ("missing", "security_fixture", anchor, at + 5 * MINUTE),
        (ids[-1], "missing", anchor, at + 5 * MINUTE),
        (ids[-1], "security_fixture", replace(anchor, timestamp_ns=at + 1), at + 5 * MINUTE),
        (ids[-1], "security_fixture", anchor, at + MINUTE),
    ]:
        with pytest.raises(ValidationError):
            engine.study_event(event_id, instrument, chosen, window(), data_as_of_ns=cutoff)
    with pytest.raises(ValidationError):
        engine.study_event(
            ids[-1],
            "security_fixture",
            EventAnchor(AnchorSemantics.SCHEDULED, AnchorQuality.DATE_ONLY, session_label="9"),
            window(),
            data_as_of_ns=at + 5 * MINUTE,
        )
    requests = ((ids[-1], "security_fixture", anchor, window()),)
    batch = engine.study_events(requests, data_as_of_ns=at + 5 * MINUTE)
    assert len(batch) == 1 and run_manifest(batch).study_ids == (batch[0].study_id,)
    with pytest.raises(ValidationError):
        engine.study_events(requests * 2, data_as_of_ns=at + 5 * MINUTE)
    with pytest.raises(ValidationError):
        run_manifest(())
    with pytest.raises(ValidationError):
        run_manifest(
            (
                batch[0],
                replace(
                    batch[0], definition=replace(batch[0].definition, event_build_id="another")
                ),
            )
        )


def test_validation_recomputes_outputs_and_checks_build(historical):
    _, reader, ids = historical
    r = result(reader, ids[-1])
    assert (
        validate_study(replace(r, post_return=99), reader).status is StudyValidationStatus.INVALID
    )
    assert validate_study(
        replace(r, definition=replace(r.definition, event_dataset_fingerprint="b" * 64)), reader
    ).reasons == ("EVENT_DATASET_MISMATCH",)
    assert (
        validate_study(replace(r, contamination=()), reader).status is StudyValidationStatus.INVALID
    )
    bad_data = replace(
        r.market,
        bars=tuple(replace(b, available_ns=r.definition.data_as_of_ns + 1) for b in r.market.bars),
    )
    assert validate_study(replace(r, market=bad_data)).status is StudyValidationStatus.INVALID


def test_documented_offline_example(tmp_path):
    output = demo(tmp_path)
    assert output["prior_count"] == 3 and output["restart_byte_identical"]
    assert output["single_half_response_ns"] == output["class_half_response_ns"] == 2 * MINUTE
