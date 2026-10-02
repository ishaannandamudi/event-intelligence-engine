from dataclasses import FrozenInstanceError, replace
from math import inf, nan

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.persistence import codec
from eie.studies.checks import matches
from eie.studies.market import (
    Adjustment,
    IndexedMarketProvider,
    MarketBar,
    MarketGap,
    MarketQuote,
    MarketSession,
    OptionsMarketObservation,
    ReturnType,
    SplitBoundary,
    Timeframe,
)
from eie.studies.models import (
    AnchorQuality,
    AnchorSemantics,
    ContextLabel,
    EventAnchor,
    ReturnMethod,
    StudyPolicy,
)
from eie.studies.returns import raw_return
from examples.event_studies import MINUTE, market
from tests.study_helpers import window


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", nan),
        ("high", inf),
        ("close", -1),
        ("low", 101),
        ("high", 90),
        ("volume", -1),
        ("start_ns", True),
        ("end_ns", 0),
        ("available_ns", 0),
        ("instrument_id", ""),
        ("source", " source "),
        ("timeframe", "1m"),
        ("end_ns", 2 * MINUTE + 1),
        ("close", True),
    ],
)
def test_bar_validation(field, value):
    bar = MarketBar(
        "security_fixture", 0, MINUTE, 100, 101, 99, 100, 10, MINUTE, "SYNTHETIC", Timeframe.M1
    )
    with pytest.raises(ValidationError):
        replace(bar, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("bid", -1),
        ("ask", 0),
        ("bid_size", -1),
        ("ask_size", inf),
        ("available_ns", 0),
        ("timestamp_ns", False),
    ],
)
def test_quote_validation(field, value):
    quote = MarketQuote("s", 1, 1, 100, 101, 10, 20, "S")
    with pytest.raises(ValidationError):
        replace(quote, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("expiry", "2026-99-10"),
        ("expiry", "20260101"),
        ("iv", -1),
        ("volume", -1),
        ("open_interest", -1),
        ("skew", nan),
        ("available_ns", 0),
    ],
)
def test_options_validation(field, value):
    option = OptionsMarketObservation("s", 1, 1, "2026-12-01", "1M", "ATM", 0.2, -0.03, 10, 20, "S")
    with pytest.raises(ValidationError):
        replace(option, **{field: value})


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_explicit_timeframe(timeframe):
    assert timeframe.duration_ns > 0
    bar = MarketBar(
        "s", 0, timeframe.duration_ns, 1, 1, 1, 1, 0, timeframe.duration_ns, "S", timeframe
    )
    assert codec.loads(codec.dumps(bar)) == bar
    with pytest.raises(FrozenInstanceError):
        bar.close = 2


@pytest.mark.parametrize(
    "adjustment,return_type,valid",
    [
        (Adjustment.UNADJUSTED, ReturnType.PRICE_RETURN, True),
        (Adjustment.SPLIT_ADJUSTED, ReturnType.PRICE_RETURN, True),
        (Adjustment.SPLIT_DIVIDEND_ADJUSTED, ReturnType.TOTAL_RETURN, True),
        (Adjustment.UNADJUSTED, ReturnType.TOTAL_RETURN, False),
        (Adjustment.SPLIT_ADJUSTED, ReturnType.TOTAL_RETURN, False),
    ],
)
def test_price_total_return_contract(adjustment, return_type, valid):
    if valid:
        assert replace(market().identity, adjustment=adjustment, return_type=return_type)
    else:
        with pytest.raises(ValidationError):
            replace(market().identity, adjustment=adjustment, return_type=return_type)


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_fingerprint": "z" * 64},
        {"instrument_id": "s" * 257},
        {"source": "bad\nlabel"},
        {"bars": []},
        {"bars": tuple(reversed(market().bars[:3]))},
        {"bars": (market().bars[0], market().bars[0])},
        {"bars": (replace(market().bars[0], source="other"),)},
        {"quotes": (MarketQuote("other", 1, 1, 1, 1, 0, 0, "SYNTHETIC"),)},
        {
            "sessions": (
                MarketSession("x", 0, MINUTE, 0, "S", "1"),
                MarketSession("x", MINUTE, 2 * MINUTE, 0, "S", "1"),
            )
        },
        {"gaps": (MarketGap(2, 3, "x"), MarketGap(0, 1, "x"))},
        {"splits": (SplitBoundary(2, 2, "S", "b"), SplitBoundary(1, 1, "S", "a"))},
    ],
)
def test_series_validation(changes):
    data = market()
    target = (
        data.identity
        if next(iter(changes)) in ("dataset_fingerprint", "instrument_id", "source")
        else data
    )
    with pytest.raises(ValidationError):
        replace(target, **changes)


def test_market_provider_indexed_ranges_and_missing_instrument():
    data = market()
    provider = IndexedMarketProvider((data,))
    assert provider.identity("missing") is None
    assert provider.sessions("security_fixture") == data.sessions
    subset = provider.read("security_fixture", data.bars[10].start_ns, data.bars[12].end_ns)
    assert subset.bars == data.bars[10:14]
    assert provider.read("security_fixture", 0, 1).bars == ()
    for args in [("missing", 0, 1), ("security_fixture", 2, 1)]:
        with pytest.raises(ValidationError):
            provider.read(*args)
    with pytest.raises(ValidationError):
        IndexedMarketProvider((data, data))
    with pytest.raises(ValidationError):
        IndexedMarketProvider([data])
    assert codec.loads(codec.dumps(data)) == data


@pytest.mark.parametrize(
    "quality", [AnchorQuality.DATE_ONLY, AnchorQuality.SESSION_LABEL, AnchorQuality.UNKNOWN]
)
def test_coarse_time_cannot_carry_exact_timestamp(quality):
    with pytest.raises(ValidationError):
        EventAnchor(AnchorSemantics.SCHEDULED, quality, 1)
    if quality is not AnchorQuality.UNKNOWN:
        with pytest.raises(ValidationError):
            EventAnchor(AnchorSemantics.SCHEDULED, quality)


@pytest.mark.parametrize(
    "changes",
    [
        {"pre_ns": 0},
        {"post_ns": -1},
        {"offsets_ns": ()},
        {"offsets_ns": (1, 0)},
        {"offsets_ns": (0, 0)},
        {"offsets_ns": (20 * MINUTE,)},
        {"coarse_policy": "REJECT"},
        {"offsets_ns": tuple(range(1001))},
        {"pre_ns": True},
    ],
)
def test_window_validation(changes):
    with pytest.raises(ValidationError):
        replace(window(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"minimum_volatility_returns": 0},
        {"minimum_baseline_bars": 0},
        {"max_lag_ns": -1},
        {"volume_baseline_ns": False},
        {"options_key": ("x",)},
    ],
)
def test_policy_validation(changes):
    with pytest.raises(ValidationError):
        replace(StudyPolicy(), **changes)


@pytest.mark.parametrize("method", list(ReturnMethod))
def test_returns_numerical_guards(method):
    assert raw_return(None, 1, method) is None
    assert raw_return(0, 1, method) is None
    assert raw_return(1, -1, method) is None
    assert raw_return(1, 0, method) == (None if method is ReturnMethod.LOG else -1)
    assert raw_return(1e-300, 1e300, method) == (
        None if method is ReturnMethod.SIMPLE else pytest.approx(1381.5510557964274)
    )
    assert raw_return(100, 110, method) == pytest.approx(
        0.1 if method is ReturnMethod.SIMPLE else 0.095310179804325
    )


def test_typed_tuple_and_context_guards():
    assert not matches([], tuple[str, ...])
    assert not matches((1,), tuple[str, ...])
    assert not matches(1, str | None)
    assert matches(None, str | None)
    assert not matches(1, frozenset[str])
    with pytest.raises(ValidationError):
        ContextLabel("regime", "stable", "S", "1", 2, 1)
    with pytest.raises(ValidationError):
        MarketGap(1, 1, "x")
    with pytest.raises(ValidationError):
        MarketSession("x", 1, 1, 0, "S", "1")
    with pytest.raises(PersistenceError):
        codec.loads(b'{"type":"os.system","fields":{}}')
