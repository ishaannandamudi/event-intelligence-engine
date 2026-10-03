"""Extract real retained study/impact responses under an explicit compatible cohort."""

from math import fsum, isfinite

from eie.analogues.models import FeatureRow, ReactionDistribution, ResponseObservation, ResponseSpec
from eie.analogues.schema import ResponseMetric
from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.impact.models import EventImpactResult
from eie.studies.aggregation import quantile
from eie.studies.models import EventStudyResult
from eie.studies.numerics import median


def study_cohort(study: EventStudyResult) -> str:
    d, m = study.definition, study.market.identity
    return fingerprint(
        "eie:analogue-study-cohort:v1",
        (
            d.event_build_id,
            d.event_dataset_fingerprint,
            d.window,
            d.policy,
            m.category,
            m.timeframe,
            m.adjustment,
            m.return_type,
            d.schema_version,
        ),
    )


def model_cohort(impact: EventImpactResult) -> str:
    s, i = impact.model_spec, impact.inputs
    # Per-event model IDs remain pinned on observations. Cohorts compare the complete
    # method/configuration and common benchmark/factor identities, not fitted outcomes.
    return fingerprint(
        "eie:analogue-model-cohort:v1",
        (
            s.model_type,
            s.estimation_window,
            s.event_window,
            s.car_windows,
            (
                s.semantics.timeframe,
                s.semantics.adjustment,
                s.semantics.return_type,
                s.semantics.method,
                s.semantics.period_definition,
            ),
            tuple(sorted({(session.source, session.version) for session in i.target.sessions})),
            s.fit_method,
            s.minimum_observations,
            s.intercept,
            s.contamination_policy,
            s.exclude_fit_contamination,
            s.matching,
            s.confidence_level,
            s.minimum_uncertainty_observations,
            s.low_r2_threshold,
            s.maximum_condition,
            s.model_version,
            s.schema_version,
            None
            if i.benchmark is None
            else (
                i.benchmark.identity.instrument_id,
                i.benchmark.identity.category,
                i.benchmark.identity.source,
                i.benchmark.identity.dataset_id,
                i.benchmark.identity.version,
                i.benchmark.identity.timeframe,
                i.benchmark.identity.adjustment,
                i.benchmark.identity.return_type,
            ),
            tuple(
                (f.factor_id, f.source, f.dataset_id, f.dataset_fingerprint, f.version)
                for f in i.factors
            ),
            s.universe,
        ),
    )


def response_spec(
    study: EventStudyResult,
    metric: ResponseMetric = ResponseMetric.RAW_RETURN,
    *,
    offset_ns: int | None = None,
    impact: EventImpactResult | None = None,
    car_label: str | None = None,
) -> ResponseSpec:
    return ResponseSpec(
        metric,
        study_cohort(study),
        study.definition.window.post_ns if offset_ns is None else offset_ns,
        None if impact is None else model_cohort(impact),
        car_label,
    )


def extract_response(
    row: FeatureRow,
    study: EventStudyResult,
    spec: ResponseSpec,
    impact: EventImpactResult | None = None,
) -> ResponseObservation:
    event = study.definition.event
    if (
        event.event_id != row.event_id
        or event.revision.number != row.revision
        or study.definition.event_build_id != row.build_id
        or study.definition.event_dataset_fingerprint != row.dataset_fingerprint
        or study_cohort(study) != spec.study_cohort
        or spec.offset_ns > study.definition.window.post_ns
        or (impact is not None) != (spec.model_cohort is not None)
    ):
        raise ValidationError("response event/revision/build/window/cohort mismatch")
    if impact is not None and (
        impact.inputs.study != study or model_cohort(impact) != spec.model_cohort
    ):
        raise ValidationError("exact compatible retained impact model required")
    value = None
    completed = study.post_end_ns
    available = study.available_ns
    quality = {q.value for q in study.quality}
    reconstruction = set(row.reconstruction_reasons)
    if study.definition.event_gap_fingerprints:
        reconstruction.add("EVENT_HISTORY_GAPS")
    reconstruction.add(study.definition.reconstruction_quality.value)
    match spec.metric:
        case ResponseMetric.RAW_RETURN | ResponseMetric.ABS_RAW_RETURN:
            value = next((p.value for p in study.curve if p.offset_ns == spec.offset_ns), None)
        case ResponseMetric.ABNORMAL_RETURN | ResponseMetric.ABS_ABNORMAL_RETURN:
            assert impact is not None
            value = next(
                (p.abnormal_return for p in impact.abnormal_curve if p.offset_ns == spec.offset_ns),
                None,
            )
        case ResponseMetric.CAR:
            assert impact is not None
            car = next((c for c in impact.cars if c.window.label == spec.car_label), None)
            if car is None or car.window.end_offset != spec.offset_ns:
                raise ValidationError("explicit CAR window/offset required")
            value = car.value
        case ResponseMetric.REALIZED_VOL_CHANGE:
            value = study.volatility.difference
        case ResponseMetric.RELATIVE_VOLUME:
            value = study.volume[-1].relative if study.volume else None
        case ResponseMetric.IV_CHANGE:
            value = study.options.iv_change
        case ResponseMetric.SPREAD_CHANGE:
            if study.liquidity.pre_spread is not None and study.liquidity.post_spread is not None:
                value = study.liquidity.post_spread - study.liquidity.pre_spread
    if (
        spec.metric
        not in (
            ResponseMetric.RAW_RETURN,
            ResponseMetric.ABS_RAW_RETURN,
            ResponseMetric.ABNORMAL_RETURN,
            ResponseMetric.ABS_ABNORMAL_RETURN,
            ResponseMetric.CAR,
        )
        and spec.offset_ns != study.definition.window.post_ns
    ):
        raise ValidationError("aggregate metric requires the complete declared study window")
    if value is not None and spec.metric in (
        ResponseMetric.ABS_RAW_RETURN,
        ResponseMetric.ABS_ABNORMAL_RETURN,
    ):
        value = abs(value)
    reason = "METRIC_NOT_AVAILABLE" if value is None else None
    if value is not None and not isfinite(value):
        value, reason = None, "NUMERIC_OVERFLOW"
    if impact is not None:
        quality.update(q.value for q in impact.quality)
        available = max(available, impact.available_ns)
    return ResponseObservation(
        row.row_id,
        row.event_id,
        row.revision,
        spec,
        study.study_id,
        study.result_fingerprint,
        None if impact is None else impact.impact_id,
        None if impact is None else impact.model_spec.model_id,
        None if impact is None else impact.result_fingerprint,
        value,
        completed,
        available,
        bool(study.contamination),
        tuple(sorted(quality)),
        tuple(sorted(reconstruction)),
        reason,
    )


def distribution(
    spec: ResponseSpec,
    members: tuple[tuple[str, float], ...],
    minimum_history: int,
) -> ReactionDistribution:
    values = tuple(v for _, v in members)
    avg = center = mad = pos = neg = absolute = low = high = None
    quantiles: tuple[tuple[float, float], ...] = ()
    reasons = []
    if len(values) < minimum_history:
        reasons.append("INSUFFICIENT_HISTORY")
    else:
        try:
            center = median(values)
            avg = fsum(v / len(values) for v in values)
            mad = median(abs(v - center) for v in values)
            if not isfinite(avg) or not isfinite(mad):
                raise OverflowError
            absolute = median(abs(v) for v in values)
            pos, neg = (
                sum(v > 0 for v in values) / len(values),
                sum(v < 0 for v in values) / len(values),
            )
            low, high = min(values), max(values)
            quantiles = tuple((p, quantile(values, p)) for p in (0.1, 0.25, 0.5, 0.75, 0.9))
        except (OverflowError, ValidationError):
            avg = center = mad = absolute = pos = neg = low = high = None
            quantiles = ()
            reasons.append("NUMERIC_OVERFLOW")
    return ReactionDistribution(
        spec,
        tuple(i for i, _ in members),
        values,
        avg,
        center,
        quantiles,
        mad,
        pos,
        neg,
        absolute,
        low,
        high,
        tuple(reasons),
    )
