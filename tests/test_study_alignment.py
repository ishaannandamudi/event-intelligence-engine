from dataclasses import replace
from math import log, sqrt

import pytest

from eie.core.errors import ValidationError
from eie.studies.alignment import align, point_bar
from eie.studies.market import MarketBar, MarketSession, MarketSlice, Timeframe
from eie.studies.models import (
    AlignmentPolicy,
    AnchorQuality,
    AnchorSemantics,
    CoarsePolicy,
    EventAnchor,
    ReferencePolicy,
    SessionPolicy,
    StudyPolicy,
    StudyQuality,
)
from eie.studies.volatility import realized, volatility
from eie.studies.windows import offset_time, resolve_anchor
from examples.event_studies import MINUTE, market
from tests.study_helpers import window


def small_bars():
    return tuple(
        MarketBar(
            "security_fixture",
            i * MINUTE,
            (i + 1) * MINUTE,
            100 + i,
            100 + i,
            100 + i,
            100 + i,
            10,
            (i + 1) * MINUTE,
            "SYNTHETIC",
            Timeframe.M1,
        )
        for i in range(10)
    )


@pytest.mark.parametrize("reference", list(ReferencePolicy))
def test_reference_price_policy(reference):
    data = MarketSlice(market().identity, small_bars())
    a = align(
        data, 0, 5 * MINUTE, 10 * MINUTE, StudyPolicy(reference=reference), SessionPolicy.ELAPSED
    )
    assert a.reference_price == (104 if reference is ReferencePolicy.PRE_CLOSE else 105)
    assert a.reference_ns == (
        5 * MINUTE if reference is not ReferencePolicy.ALIGNED_CLOSE else 6 * MINUTE
    )
    assert all(b.end_ns <= 5 * MINUTE for b in a.pre)
    assert all(b.start_ns >= 5 * MINUTE for b in a.post)


def test_spanning_future_partial_and_missing_bars():
    data = MarketSlice(market().identity, small_bars())
    anchor = 5 * MINUTE + MINUTE // 2
    a = align(data, MINUTE // 2, anchor, 9 * MINUTE, StudyPolicy(), SessionPolicy.ELAPSED)
    assert StudyQuality.PARTIAL_SESSION in a.quality
    assert StudyQuality.DATA_GAP in a.quality
    assert small_bars()[5] not in a.pre and small_bars()[5] not in a.post
    delayed = replace(data, bars=tuple(replace(b, available_ns=20 * MINUTE) for b in data.bars))
    assert not align(delayed, 0, 5 * MINUTE, 10 * MINUTE, StudyPolicy(), SessionPolicy.ELAPSED).pre
    partial = replace(data, bars=(replace(data.bars[0], end_ns=MINUTE // 2),))
    assert (
        StudyQuality.PARTIAL_SESSION
        in align(partial, 0, MINUTE, 2 * MINUTE, StudyPolicy(), SessionPolicy.ELAPSED).quality
    )


@pytest.mark.parametrize("policy", list(AlignmentPolicy))
def test_point_alignment_explicit_missing(policy):
    bars = small_bars()
    p = StudyPolicy(alignment=policy)
    b = point_bar(bars, 3 * MINUTE, 5 * MINUTE, 10 * MINUTE, p, SessionPolicy.ELAPSED, ())
    assert b == bars[2 if policy is AlignmentPolicy.LAST_COMPLETED else 3]
    assert point_bar((), 3 * MINUTE, 5 * MINUTE, 10 * MINUTE, p, SessionPolicy.ELAPSED, ()) is None
    if policy is AlignmentPolicy.LAST_COMPLETED:
        assert (
            point_bar(bars[:2], 3 * MINUTE, 5 * MINUTE, 10 * MINUTE, p, SessionPolicy.ELAPSED, ())
            is None
        )
    else:
        assert (
            point_bar(bars[5:], 4 * MINUTE, 5 * MINUTE, 10 * MINUTE, p, SessionPolicy.ELAPSED, ())
            is None
        )


def test_trading_clock_skips_only_supplied_closures():
    sessions = (
        MarketSession("a", 0, 10, 0, "S", "1"),
        MarketSession("b", 20, 30, 0, "S", "1"),
        MarketSession("c", 40, 50, 0, "S", "1"),
    )
    assert offset_time(8, 5, SessionPolicy.TRADING, sessions) == 23
    assert offset_time(22, -5, SessionPolicy.TRADING, sessions) == 7
    assert offset_time(15, 3, SessionPolicy.TRADING, sessions) == 23
    assert offset_time(35, -3, SessionPolicy.TRADING, sessions) == 27
    assert offset_time(8, 5, SessionPolicy.ELAPSED, sessions) == 13
    assert offset_time(8, 0, SessionPolicy.TRADING, ()) == 8
    for offset in (-100, 100):
        with pytest.raises(ValidationError):
            offset_time(8, offset, SessionPolicy.TRADING, sessions)
    with pytest.raises(ValidationError):
        offset_time(8, 5, SessionPolicy.TRADING, (replace(sessions[1], available_ns=100),))


@pytest.mark.parametrize("coarse", [CoarsePolicy.SESSION_OPEN, CoarsePolicy.SESSION_CLOSE])
def test_coarse_session_alignment_preserves_quality(coarse):
    anchor = EventAnchor(AnchorSemantics.SCHEDULED, AnchorQuality.SESSION_LABEL, session_label="a")
    w = replace(window(), anchor_semantics=AnchorSemantics.SCHEDULED, coarse_policy=coarse)
    sessions = (MarketSession("a", 100, 200, 0, "S", "1"),)
    assert resolve_anchor(anchor, w, sessions) == (
        100 if coarse is CoarsePolicy.SESSION_OPEN else 200
    )
    with pytest.raises(ValidationError):
        resolve_anchor(anchor, replace(w, coarse_policy=CoarsePolicy.REJECT), sessions)
    with pytest.raises(ValidationError):
        resolve_anchor(anchor, w, ())
    with pytest.raises(ValidationError):
        resolve_anchor(anchor, w, (replace(sessions[0], available_ns=300),))
    with pytest.raises(ValidationError):
        resolve_anchor(EventAnchor(AnchorSemantics.SCHEDULED, AnchorQuality.UNKNOWN), w, sessions)


def test_volatility_definition_counts_missing_and_zero_ratio():
    bars = small_bars()
    rv, n = realized(bars, (), 2)
    assert n == 9 and rv == pytest.approx(
        sqrt(sum(log(b.close / a.close) ** 2 for a, b in zip(bars, bars[1:], strict=False)))
    )
    assert realized(bars[:2], (), 2) == (None, 1)
    assert realized((bars[0], bars[2]), (), 1) == (None, 0)
    closed = (
        MarketSession("a", 0, MINUTE, 0, "S", "1"),
        MarketSession("b", 2 * MINUTE, 3 * MINUTE, 0, "S", "1"),
    )
    assert realized((bars[0], bars[2]), closed, 1)[1] == 1
    zeros = tuple(replace(b, open=0, low=0, high=0, close=0) for b in bars)
    assert realized(zeros, (), 1) == (None, 0)
    flat = tuple(replace(b, open=100, low=100, high=100, close=100) for b in bars)
    v = volatility(flat, flat, (), 2)
    assert v.pre == v.post == 0 and v.ratio is None and v.difference == 0
