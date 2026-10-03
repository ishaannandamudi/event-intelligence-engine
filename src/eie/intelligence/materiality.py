"""Explicit policy contributions from supplied information, never market reactions."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite

from eie.corporate.actions import Buyback, Deal, Dividend, TenderOffer
from eie.evidence.claims import ActionValue, NumericRange, StatusValue
from eie.evidence.resolution import compatible as values_compatible
from eie.evidence.schema import ClaimStatus, ResolutionState
from eie.expectations.units import CanonicalUnit, Dimension, Quantity
from eie.intelligence.inputs import IntelligenceInputs
from eie.intelligence.models import (
    EventMateriality,
    MaterialityComponent,
    NoveltyComponent,
    StaticImportance,
)
from eie.intelligence.novelty import entity_ids
from eie.intelligence.numeric import bounds, metric
from eie.intelligence.policy import IntelligencePolicy
from eie.intelligence.schema import MaterialityCategory as C
from eie.intelligence.schema import MaterialityKind as K
from eie.intelligence.schema import NoveltyDimension

CATEGORIES = (C.MINOR, C.MODERATE, C.MAJOR, C.VERY_HIGH)


@dataclass(frozen=True, slots=True)
class MagnitudeInput:
    identity: str
    low: float | None
    high: float | None
    point: float | None
    unit: CanonicalUnit


def importance(data: IntelligenceInputs, policy: IntelligencePolicy) -> StaticImportance:
    event_type = data.event.identity.event_type
    overrides = dict(policy.importance.event_types)
    category = overrides.get(event_type.code, dict(policy.importance.families)[event_type.family])
    return StaticImportance(
        category,
        "EVENT_TYPE" if event_type.code in overrides else "EVENT_FAMILY",
        policy.fingerprint,
    )


def category(value: float, thresholds: tuple[float, float, float]) -> C:
    return CATEGORIES[sum(abs(value) >= threshold for threshold in thresholds)]


def range_category(
    low: float | None,
    high: float | None,
    point: float | None,
    thresholds: tuple[float, float, float] | None,
) -> tuple[C, tuple[str, ...]]:
    if any(v is not None and not isfinite(v) for v in (low, high, point)):
        return C.UNKNOWN, ("NUMERIC_OVERFLOW",)
    if thresholds is None:
        return C.UNKNOWN, ("NO_UNIT_THRESHOLD_POLICY",)
    if low is None and high is None and point is not None:
        return category(point, thresholds), ()
    if low is None or high is None:
        return C.UNKNOWN, ("OPEN_RANGE",)
    # Magnitude can cross zero; endpoints alone would conceal the smaller outcomes.
    minimum = 0.0 if low <= 0 <= high else min(abs(low), abs(high))
    a, b = category(minimum, thresholds), category(max(abs(low), abs(high)), thresholds)
    return (a, ()) if a == b else (C.UNKNOWN, ("RANGE_CROSSES_CATEGORIES",))


def magnitudes(data: IntelligenceInputs) -> tuple[MagnitudeInput, ...]:
    result: dict[str, MagnitudeInput] = {}
    linked_amount_events: set[str | None] = set()
    for doc in data.documents:
        for c in doc.candidates:
            for n in c.proposal.numerics:
                if n.name.value != "AMOUNT":
                    continue
                from eie.nlp.schema import NumericBound

                low = (
                    float(n.quantity.canonical_value)
                    if n.bound
                    in (
                        NumericBound.POINT,
                        NumericBound.CLOSED_RANGE,
                        NumericBound.AT_LEAST,
                        NumericBound.MORE_THAN,
                    )
                    else None
                )
                high = (
                    float(n.upper.canonical_value)
                    if n.upper is not None
                    else (
                        float(n.quantity.canonical_value)
                        if n.bound
                        in (NumericBound.POINT, NumericBound.AT_MOST, NumericBound.LESS_THAN)
                        else None
                    )
                )
                result[c.candidate_id] = MagnitudeInput(
                    c.candidate_id,
                    low,
                    high,
                    float(n.quantity.canonical_value) if n.bound is NumericBound.POINT else None,
                    n.quantity.canonical_unit,
                )
                linked_amount_events.update(
                    a.event_id for a in doc.run.accepted if a.candidate_id == c.candidate_id
                )
    for f in data.current:
        r = f.assertion
        if (
            f.resolution.state is not ResolutionState.RESOLVED
            or f.resolution.value is None
            or not values_compatible(r.value, f.resolution.value)
        ) or r.status in (
            ClaimStatus.RETRACTED,
            ClaimStatus.WITHDRAWN,
        ):
            continue
        value = r.value
        if (
            isinstance(value, (Quantity, NumericRange))
            and f.claim.claim_type.value != "ESTIMATE_VALUE"
        ):
            if value.canonical_unit.dimension is Dimension.CURRENCY:
                low, high, point = bounds(value)
                result[r.observation_id] = MagnitudeInput(
                    r.observation_id, low, high, point, value.canonical_unit
                )
        elif isinstance(value, ActionValue) and r.event_id not in linked_amount_events:
            payload = value.payload
            amount = (
                payload.authorization
                if isinstance(payload, Buyback)
                else payload.cash_component
                if isinstance(payload, Deal)
                else payload.amount
                if isinstance(payload, Dividend)
                else payload.price
                if isinstance(payload, TenderOffer)
                else None
            )
            if amount is not None:
                unit = CanonicalUnit(
                    Dimension.CURRENCY_PER_SHARE if amount.per_share else Dimension.CURRENCY,
                    amount.currency,
                )
                point = amount.amount_micros / 1_000_000
                result[r.observation_id] = MagnitudeInput(
                    r.observation_id, point, point, point, unit
                )
    return tuple(result[k] for k in sorted(result))


def calculate_materiality(
    data: IntelligenceInputs, novelty: tuple[NoveltyComponent, ...], policy: IntelligencePolicy
) -> EventMateriality:
    components: list[MaterialityComponent] = []
    amounts = magnitudes(data)
    denominator = data.scale.assertion.value if data.scale is not None else None
    assert denominator is None or isinstance(denominator, Quantity)
    for amount in amounts:
        thresholds = (
            dict(policy.materiality.currency_thresholds).get(amount.unit.currency or "")
            if amount.unit.dimension is Dimension.CURRENCY
            else None
        )
        cat, reasons = range_category(amount.low, amount.high, amount.point, thresholds)
        components.append(
            MaterialityComponent(
                K.FINANCIAL_AMOUNT,
                amount.identity,
                cat,
                (
                    metric("high", amount.high, amount.unit, "BOUND_NOT_SUPPLIED"),
                    metric("low", amount.low, amount.unit, "BOUND_NOT_SUPPLIED"),
                    metric("point", amount.point, amount.unit, "RANGE_NOT_POINT"),
                ),
                (amount.identity,),
                reasons,
            )
        )
        compatible = denominator is not None and denominator.canonical_unit == amount.unit
        positive = (
            compatible
            and denominator is not None
            and denominator.canonical_value > policy.novelty.near_zero
        )

        def divide(value: float | None, eligible: bool = positive) -> float | None:
            return (
                value / float(denominator.canonical_value)
                if eligible and denominator is not None and value is not None
                else None
            )

        low, high, point = divide(amount.low), divide(amount.high), divide(amount.point)
        ratio_cat, ratio_reasons = range_category(
            low, high, point, policy.materiality.ratio_thresholds
        )
        reason = (
            "DENOMINATOR_MISSING"
            if denominator is None
            else "DENOMINATOR_UNIT_MISMATCH"
            if not compatible
            else "DENOMINATOR_NEAR_ZERO"
            if not positive
            else None
        )
        components.append(
            MaterialityComponent(
                K.COMPANY_SCALE,
                amount.identity,
                ratio_cat,
                tuple(
                    metric(name, v, reason=reason or "BOUND_NOT_SUPPLIED")
                    for name, v in (
                        ("high_ratio", high),
                        ("low_ratio", low),
                        ("point_ratio", point),
                    )
                ),
                tuple(
                    sorted(
                        (amount.identity,)
                        if data.scale is None
                        else (amount.identity, data.scale.assertion.observation_id)
                    )
                ),
                (reason,) if reason else ratio_reasons,
            )
        )
    if not amounts:
        for kind, reason in (
            (K.FINANCIAL_AMOUNT, "FINANCIAL_AMOUNT_NOT_SUPPLIED"),
            (K.COMPANY_SCALE, "DENOMINATOR_OR_AMOUNT_MISSING"),
        ):
            components.append(
                MaterialityComponent(kind, data.event.event_id, C.UNKNOWN, (), (), (reason,))
            )
    for c in novelty:
        if c.dimension is NoveltyDimension.GUIDANCE:
            displacement = next(
                (m.value for m in c.metrics if m.name == "relative_midpoint_change"), None
            )
            components.append(
                MaterialityComponent(
                    K.GUIDANCE_DISPLACEMENT,
                    c.subject_id,
                    C.UNKNOWN
                    if displacement is None
                    else category(displacement, policy.materiality.guidance_thresholds),
                    (
                        metric(
                            "relative_midpoint_change",
                            displacement,
                            reason="GUIDANCE_COMPARISON_UNAVAILABLE",
                        ),
                    ),
                    c.current_ids,
                    c.reasons,
                )
            )
    scope = data.event.identity.scope
    components.append(
        MaterialityComponent(
            K.SCOPE,
            data.event.event_id,
            C.UNKNOWN,
            (
                metric(
                    "direct_scope_count",
                    float(len(scope.identifiers)) if scope.identifiers else None,
                    reason="BROAD_SCOPE_NOT_ENTITY_COUNT",
                ),
            ),
            (data.event.revision_id,),
            ("SCOPE_" + scope.kind.value,),
        )
    )
    statuses = {
        f.assertion.value.status.value
        for f in data.current
        if isinstance(f.assertion.value, StatusValue)
        and f.resolution.selected_id == f.assertion.observation_id
    }
    statuses.update(c.proposal.epistemic.value for d in data.documents for c in d.candidates)
    statuses.add(data.event.lifecycle.value)
    components.append(
        MaterialityComponent(
            K.STATUS,
            data.event.event_id,
            C.UNKNOWN,
            (),
            (data.event.revision_id,),
            tuple(sorted("STATUS_" + s for s in statuses)),
        )
    )
    ids = entity_ids(data.current)
    components.append(
        MaterialityComponent(
            K.AFFECTED_ENTITIES,
            data.event.event_id,
            C.UNKNOWN,
            (metric("stable_entity_count", float(len(ids))),),
            (data.event.revision_id,),
            ("UNRESOLVED_ENTITY_COUNT_UNKNOWN",)
            if any(d.unresolved_entity for d in data.documents)
            else (),
        )
    )
    graph = data.current_graph
    edges = (
        ()
        if graph is None
        else tuple(
            e for e in graph.edges if e.active and {e.source_node_id, e.target_node_id} & set(ids)
        )
    )
    if not edges:
        components.append(
            MaterialityComponent(
                K.EXPOSURE, data.event.event_id, C.UNKNOWN, (), (), ("EXPOSURE_NOT_SUPPLIED",)
            )
        )
    for edge in edges:
        exposure = edge.exposure
        point = None if exposure is None else exposure.point
        low = point if point is not None else None if exposure is None else exposure.lower
        high = point if point is not None else None if exposure is None else exposure.upper
        cat, reasons = range_category(low, high, point, policy.materiality.exposure_thresholds)
        components.append(
            MaterialityComponent(
                K.EXPOSURE,
                edge.edge_id,
                cat,
                (
                    metric("high", high, reason="EXPOSURE_NOT_SUPPLIED"),
                    metric("low", low, reason="EXPOSURE_NOT_SUPPLIED"),
                    metric("point", point, reason="EXPOSURE_RANGE_OR_MISSING"),
                ),
                (edge.revision_id,),
                tuple(
                    sorted(
                        set(reasons)
                        | {
                            "DIRECT_RELATIONSHIP_CONTEXT",
                            "EXPOSURE_TYPE_"
                            + (
                                exposure.measurement_type.value
                                if exposure is not None
                                else "UNKNOWN"
                            ),
                        }
                        | (
                            {"ESTIMATED_EXPOSURE"}
                            if exposure is not None and exposure.estimated
                            else set()
                        )
                        | ({"GRAPH_CONFLICT"} if edge.conflict_reasons else set())
                    )
                ),
            )
        )
    durations: list[tuple[str, float]] = []
    for f in data.current:
        value = f.assertion.value
        if (
            isinstance(value, ActionValue)
            and isinstance(value.payload, Buyback)
            and value.payload.expiration_date is not None
            and data.event.announced_at_ns is not None
        ):
            start_date = datetime.fromtimestamp(
                data.event.announced_at_ns // 1_000_000_000, UTC
            ).date()
            end_date = date.fromisoformat(value.payload.expiration_date)
            if end_date >= start_date:
                durations.append((f.assertion.observation_id, float((end_date - start_date).days)))
    if not durations:
        components.append(
            MaterialityComponent(
                K.DURATION, data.event.event_id, C.UNKNOWN, (), (), ("DURATION_NOT_SUPPLIED",)
            )
        )
    for identity, days in durations:
        components.append(
            MaterialityComponent(
                K.DURATION,
                identity,
                C.UNKNOWN,
                (metric("authorization_calendar_day_gap", days),),
                (identity,),
                ("EXPIRATION_DATE_ONLY", "TIMEZONE_UNSPECIFIED"),
            )
        )
    eligible = [
        c.category
        for c in components
        if c.kind in (K.FINANCIAL_AMOUNT, K.COMPANY_SCALE, K.GUIDANCE_DISPLACEMENT, K.EXPOSURE)
        and c.category is not C.UNKNOWN
    ]
    combined = max(eligible, key=CATEGORIES.index) if eligible else C.UNKNOWN
    missing = any(
        c.category is C.UNKNOWN
        for c in components
        if c.kind in (K.FINANCIAL_AMOUNT, K.COMPANY_SCALE, K.GUIDANCE_DISPLACEMENT, K.EXPOSURE)
    )
    return EventMateriality(
        combined,
        tuple(sorted(components, key=lambda c: (c.kind.value, c.subject_id))),
        policy.materiality.combination_rule,
        tuple(
            sorted(
                ({"PARTIAL_INPUTS"} if missing else set())
                | ({"GRAPH_TRUNCATED"} if graph is not None and graph.truncation_reasons else set())
            )
        ),
    )
