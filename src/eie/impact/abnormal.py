"""Period residuals and independent CAR windows; never sum cumulative raw curves."""

from math import fsum, isfinite

from eie.core.fingerprints import fingerprint
from eie.impact.estimation import period_returns, tuple_bounds
from eie.impact.inputs import ImpactInputs
from eie.impact.models import (
    AbnormalReturnPoint,
    BenchmarkFit,
    ContaminationPolicy,
    ControlFit,
    CounterfactualModelSpec,
    CumulativeAbnormalReturn,
    FitStatus,
    ImpactQuality,
    ModelFitResult,
    PeriodReturn,
    RegressionFit,
)
from eie.impact.uncertainty import bands
from eie.studies.alignment import active_duration
from eie.studies.models import SessionPolicy


def abnormal_curve(
    inputs: ImpactInputs, spec: CounterfactualModelSpec, fit: ModelFitResult
) -> tuple[AbnormalReturnPoint, ...]:
    s = inputs.study
    benchmark = (
        {}
        if inputs.benchmark is None
        else {
            (p.start_ns, p.end_ns): p
            for p in period_returns(inputs.benchmark, spec.semantics, s.definition.data_as_of_ns)
        }
    )
    factors = tuple({(o.start_ns, o.end_ns): o for o in f.observations} for f in inputs.factors)
    controls = {
        c.instrument_id: {
            (p.start_ns, p.end_ns): p
            for p in period_returns(c.market, spec.semantics, s.definition.data_as_of_ns)
        }
        for c in inputs.controls
        if c.market is not None
    }
    result = []
    for p in period_returns(inputs.target, spec.semantics, s.definition.data_as_of_ns):
        if (
            p.start_ns < s.pre_start_ns
            or p.end_ns > s.post_end_ns
            or p.start_ns < s.anchor_ns < p.end_ns
        ):
            continue
        key = (p.start_ns, p.end_ns)
        observed = p.value if p.end_ns > s.anchor_ns or p.available_ns <= s.anchor_ns else None
        expected = abnormal = None
        design: tuple[float, ...] = ()
        fingerprints = [p.input_fingerprint]
        quality = set()
        cutoff = s.anchor_ns if p.end_ns <= s.anchor_ns else s.definition.data_as_of_ns
        payload = fit.payload
        if fit.status is FitStatus.AVAILABLE:
            if isinstance(payload, (RegressionFit, BenchmarkFit)) and inputs.benchmark is not None:
                b = benchmark.get(key)
                if b is not None and b.available_ns <= cutoff and b.value is not None:
                    design = (
                        ((1.0, b.value) if spec.intercept else (b.value,))
                        if isinstance(payload, RegressionFit)
                        else (b.value,)
                    )
                    fingerprints.append(b.input_fingerprint)
            elif isinstance(payload, RegressionFit):
                fs = tuple(m.get(key) for m in factors)
                if all(
                    f is not None and f.available_ns <= cutoff and f.value is not None for f in fs
                ):
                    design = (
                        *((1.0,) if spec.intercept else ()),
                        *(f.value for f in fs if f is not None and f.value is not None),
                    )
                    fingerprints.extend(
                        fingerprint("eie:factor-period:v1", (series.factor_id, f))
                        for series, f in zip(inputs.factors, fs, strict=True)
                        if f is not None
                    )
            elif isinstance(payload, ControlFit):
                pairs: list[tuple[float, PeriodReturn]] = []
                for c in payload.selected:
                    cp = controls[c.instrument_id].get(key)
                    if cp is None or cp.available_ns > cutoff or cp.value is None:
                        break
                    pairs.append((c.weight, cp))
                if len(pairs) == len(payload.selected):
                    try:
                        expected = fsum(
                            weight * cp.value for weight, cp in pairs if cp.value is not None
                        )
                        fingerprints.extend(cp.input_fingerprint for _, cp in pairs)
                    except (ArithmeticError, ValueError):
                        quality.add(ImpactQuality.NUMERIC_UNAVAILABLE)
            if design:
                try:
                    expected = (
                        fsum(a * b for a, b in zip(payload.coefficients, design, strict=True))
                        if isinstance(payload, RegressionFit)
                        else design[0]
                    )
                except (ArithmeticError, ValueError):
                    quality.add(ImpactQuality.NUMERIC_UNAVAILABLE)
        if spec.contamination_policy is ContaminationPolicy.EXCLUDE_EVENT and s.contamination:
            expected = None
            quality.add(ImpactQuality.EVENT_EXCLUDED)
        if expected is not None and not isfinite(expected):
            expected = None
            quality.add(ImpactQuality.NUMERIC_UNAVAILABLE)
        if observed is not None and expected is not None:
            abnormal = observed - expected
            if not isfinite(abnormal):
                expected = abnormal = None
                quality.add(ImpactQuality.NUMERIC_UNAVAILABLE)
        if observed is None or expected is None:
            quality.add(ImpactQuality.MISSING_EVENT_WINDOW_DATA)
        expected_band = abnormal_band = None
        if abnormal is not None and expected is not None and design:
            expected_band, abnormal_band = bands(fit, spec, design, expected, abnormal)
            if isinstance(payload, RegressionFit) and abnormal_band is None:
                quality.add(ImpactQuality.UNCERTAINTY_UNAVAILABLE)
        offset = p.end_ns - s.anchor_ns
        if spec.event_window.session_policy is SessionPolicy.TRADING:
            offset = (
                active_duration(s.anchor_ns, p.end_ns, s.market.sessions)
                if p.end_ns >= s.anchor_ns
                else -active_duration(p.end_ns, s.anchor_ns, s.market.sessions)
            )
        result.append(
            AbnormalReturnPoint(
                offset,
                p.start_ns,
                p.end_ns,
                observed,
                expected,
                abnormal,
                spec.model_id,
                tuple(fingerprints),
                frozenset(quality),
                expected_band,
                abnormal_band,
                design,
            )
        )
    return tuple(result)


def cumulative_returns(
    inputs: ImpactInputs,
    spec: CounterfactualModelSpec,
    fit: ModelFitResult,
    curve: tuple[AbnormalReturnPoint, ...],
) -> tuple[CumulativeAbnormalReturn, ...]:
    result = []
    for window in spec.car_windows:
        start, end = tuple_bounds(inputs.study, window.start_offset, window.end_offset)
        points = tuple(p for p in curve if p.start_ns >= start and p.end_ns <= end)
        cursor = start
        reasons = set()
        for p in points:
            if p.start_ns != cursor:
                reasons.add("INCOMPLETE_PERIOD_COVERAGE")
            cursor = p.end_ns
        if cursor != end or not points:
            reasons.add("INCOMPLETE_PERIOD_COVERAGE")
        if any(p.abnormal_return is None for p in points):
            reasons.add("UNAVAILABLE_ABNORMAL_PERIOD")
        value = None
        uncertainty = None
        if not reasons:
            try:
                value = fsum(p.abnormal_return for p in points if p.abnormal_return is not None)
                if not isfinite(value):
                    raise OverflowError
                if isinstance(fit.payload, RegressionFit):
                    design_sum = tuple(
                        fsum(p.design[j] for p in points) for j in range(len(fit.payload.names))
                    )
                    expected = fsum(
                        p.expected_return for p in points if p.expected_return is not None
                    )
                    _, uncertainty = bands(fit, spec, design_sum, expected, value, len(points))
                    if uncertainty is None:
                        reasons.add("UNCERTAINTY_UNAVAILABLE")
            except (ArithmeticError, ValueError):
                value = None
                reasons.add("NUMERIC_UNAVAILABLE")
        result.append(
            CumulativeAbnormalReturn(
                window, start, end, value, len(points), uncertainty, tuple(sorted(reasons))
            )
        )
    return tuple(result)
