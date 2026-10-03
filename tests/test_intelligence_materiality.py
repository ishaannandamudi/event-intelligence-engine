"""Transparent amount/scale/range/status policies and independent uncertainty states."""

from dataclasses import replace

import pytest

from eie.corporate.actions import Buyback, Deal, Dividend, Split, TenderOffer
from eie.corporate.types import CorporateStatus, Money
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.claims import ActionValue, NumericRange, StatusValue
from eie.expectations.units import Quantity, Unit, UnitKind
from eie.intelligence.engine import calculate
from eie.intelligence.materiality import range_category
from eie.intelligence.policy import ImportancePolicy, IntelligencePolicy, MaterialityPolicy
from eie.intelligence.schema import Importance, MaterialityCategory
from eie.persistence.sqlite import SQLiteEventStore
from eie.sources.trust import SourceClass
from examples.intelligence_fixtures import USD, batch, source
from tests.intelligence_helpers import compute, ingest, inputs, metrics, uncertainty


def contribution(result, kind):
    return tuple(c for c in result.materiality.components if c.kind.value == kind)


@pytest.mark.parametrize(
    "value,category",
    [
        (0, "MINOR"),
        (9_999_999, "MINOR"),
        (10_000_000, "MODERATE"),
        (100_000_000, "MAJOR"),
        (1_000_000_000, "VERY_HIGH"),
        (-100_000_000, "MAJOR"),
    ],
)
def test_absolute_amount_policy_has_no_direction(value, category):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20, Quantity(value, USD))
        ingest(store, p, b)
        r = compute(store, b)
        assert contribution(r, "FINANCIAL_AMOUNT")[0].category.value == category
        assert r.materiality.category.value == category
        assert contribution(r, "COMPANY_SCALE")[0].metrics[0].value is None
        assert "DENOMINATOR_MISSING" in contribution(r, "COMPANY_SCALE")[0].reasons


@pytest.mark.parametrize(
    "denominator,reason",
    [
        (0, "DENOMINATOR_NEAR_ZERO"),
        (-10, "DENOMINATOR_NEAR_ZERO"),
        (1e-12, "DENOMINATOR_NEAR_ZERO"),
        (1000, None),
    ],
)
def test_scale_is_sourced_point_in_time_and_zero_is_not_epsilon(denominator, reason):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        scale = batch(p, 10, Quantity(denominator, USD), key="scale", measurement="EARNINGS.ASSETS")
        receipt = ingest(store, p, scale)
        b = batch(p, 20, Quantity(100, USD))
        ingest(store, p, b)
        r = compute(store, b, scale_assertion_id=receipt.assertion_ids[0])
        c = contribution(r, "COMPANY_SCALE")[0]
        assert (reason in c.reasons) if reason else metrics(c)["point_ratio"] == 0.1
        if reason:
            assert all(m.value is None for m in c.metrics)
        else:
            assert c.category.value == "MAJOR" and r.materiality.category.value == "MAJOR"
        assert c.input_ids == tuple(sorted((receipt.assertion_ids[0], r.novelty[0].current_ids[0])))


@pytest.mark.parametrize(
    "lower,upper,expected",
    [
        (1, 2, "MINOR"),
        (1, 100_000_000, "UNKNOWN"),
        (-100_000_000, 100_000_000, "UNKNOWN"),
        (100_000_000, 200_000_000, "MAJOR"),
        (None, 20, "UNKNOWN"),
        (20, None, "UNKNOWN"),
    ],
)
def test_materiality_ranges_do_not_invent_points(lower, upper, expected):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        value = NumericRange(
            None if lower is None else Quantity(lower, USD),
            None if upper is None else Quantity(upper, USD),
        )
        b = batch(p, 20, value)
        ingest(store, p, b)
        r = compute(store, b)
        c = contribution(r, "FINANCIAL_AMOUNT")[0]
        assert c.category.value == expected
        assert metrics(c)["point"] is None
        assert uncertainty(r, "NUMERIC").state.value == "MODERATE"


def test_currency_requires_explicit_policy_without_conversion():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20, Quantity(1_000_000_000, Unit(UnitKind.CURRENCY, currency="EUR")))
        ingest(store, p, b)
        r = compute(store, b)
        assert r.materiality.category.value == "UNKNOWN"
        assert contribution(r, "FINANCIAL_AMOUNT")[0].reasons == ("NO_UNIT_THRESHOLD_POLICY",)
        policy = IntelligencePolicy(
            materiality=MaterialityPolicy(currency_thresholds=(("EUR", (10.0, 100.0, 1000.0)),))
        )
        new = compute(store, b, policy=policy)
        assert (
            new.materiality.category.value == "VERY_HIGH"
            and new.intelligence_id != r.intelligence_id
        )


@pytest.mark.parametrize(
    "action,point",
    [
        (Buyback(authorization=Money(500_000_000_000_000, "USD")), 500_000_000),
        (
            Deal("iss_" + "a" * 64, None, cash_component=Money(10_000_000_000_000, "USD")),
            10_000_000,
        ),
        (Dividend(Money(1_000_000, "USD", True)), 1),
        (TenderOffer(Money(50_000_000, "USD", True)), 50),
        (Split(2, 1), None),
    ],
)
def test_corporate_amount_contracts_are_reused(action, point):
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20, ActionValue(action))
        ingest(store, p, b)
        r = compute(store, b)
        c = contribution(r, "FINANCIAL_AMOUNT")[0]
        if point is None:
            assert c.metrics == () and c.category.value == "UNKNOWN"
        else:
            assert metrics(c)["point"] == point
        if isinstance(action, (Dividend, TenderOffer)):
            assert c.category.value == "UNKNOWN"


def test_buyback_duration_retains_date_precision_and_status_is_not_confirmation_upgrade():
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(
            p,
            20,
            ActionValue(
                Buyback(authorization=Money(1_000_000, "USD"), expiration_date="2026-10-01")
            ),
        )
        b = replace(b, events=(replace(b.events[0], announced_at_ns=20),))
        ingest(store, p, b)
        r = compute(store, b)
        duration = contribution(r, "DURATION")[0]
        assert metrics(duration)["authorization_calendar_day_gap"] > 0 and duration.reasons == (
            "EXPIRATION_DATE_ONLY",
            "TIMEZONE_UNSPECIFIED",
        )
        status = batch(p, 30, StatusValue(CorporateStatus.PENDING), key="pending")
        ingest(store, p, status)
        pending = compute(store, status)
        assert pending.materiality.category.value == "UNKNOWN"
        assert "STATUS_PENDING" in contribution(pending, "STATUS")[0].reasons
        assert "NON_FINAL_OR_DISPUTED_STATUS" in uncertainty(pending, "MATERIALITY").reasons


@pytest.mark.parametrize("family", list(EventFamily))
def test_static_family_importance_and_explicit_override(family):
    code = (
        "MACRO.TEST"
        if family is EventFamily.MACRO
        else "SEC.TEST"
        if family is EventFamily.SEC_FILING
        else "MANAGEMENT.TEST"
        if family is EventFamily.MANAGEMENT_GOVERNANCE
        else "PRODUCT.TEST"
        if family is EventFamily.PRODUCT_BUSINESS
        else family.value + ".TEST"
    )
    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20, event_type=EventType(family, code))
        ingest(store, p, b)
        first = compute(store, b)
        policy = IntelligencePolicy(
            importance=ImportancePolicy(event_types=((code, Importance.CRITICAL),))
        )
        second = compute(store, b, policy=policy)
        assert second.static_importance.category is Importance.CRITICAL
        assert (
            second.static_importance.rule == "EVENT_TYPE"
            and second.intelligence_id != first.intelligence_id
        )
        assert second.materiality == first.materiality and second.novelty == first.novelty


def test_source_uncertainty_does_not_replace_entity_or_numeric_uncertainty():
    with SQLiteEventStore(":memory:") as store:
        p = source(source_class=SourceClass.UNVERIFIED)
        b = batch(p, 20)
        ingest(store, p, b)
        r = compute(store, b)
        assert uncertainty(r, "SOURCE").state.value == "HIGH"
        assert uncertainty(r, "ENTITY").state.value == "LOW"
        assert uncertainty(r, "NUMERIC").state.value == "LOW"
        assert uncertainty(r, "CLASSIFICATION").state.value == "UNKNOWN"
        assert uncertainty(r, "ANALOGUE").state.value == "INSUFFICIENT_INPUTS"
        assert uncertainty(r, "IMPACT").state.value == "NOT_COMPUTED"
        assert len(r.uncertainty) == 8 and not hasattr(r, "confidence")


def test_operational_bounds_do_not_enter_result_policy_identity():
    from eie.intelligence.policy import IntelligenceLimits

    with SQLiteEventStore(":memory:") as store:
        p = source()
        b = batch(p, 20)
        ingest(store, p, b)
        data = inputs(store, b)
        assert calculate(data, IntelligencePolicy()) == calculate(
            data, IntelligencePolicy(), IntelligenceLimits(query_size=1, prior_claims=10)
        )


def test_range_category_empty_inputs_are_explicit():
    assert range_category(None, None, None, (1, 2, 3)) == (
        MaterialityCategory.UNKNOWN,
        ("OPEN_RANGE",),
    )
