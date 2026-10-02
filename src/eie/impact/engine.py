"""Validated raw study → pinned causal model → immutable counterfactual estimate."""

from math import fsum, isfinite

from eie.core.errors import ValidationError
from eie.impact.abnormal import abnormal_curve, cumulative_returns
from eie.impact.diagnostics import quality_flags
from eie.impact.estimation import dataset_pins, project_inputs, semantics, verify_inputs
from eie.impact.fitting import fit_model
from eie.impact.inputs import ImpactInputs
from eie.impact.models import (
    CARWindow,
    ContaminationPolicy,
    ControlFit,
    CounterfactualModelSpec,
    EstimationWindow,
    EventImpactResult,
    FitMethod,
    FitStatus,
    ImpactQuality,
    ImpactRunManifest,
    MatchingPolicy,
    MetricCounterfactual,
    ModelFitResult,
    ModelType,
)
from eie.impact.uncertainty import ASSUMPTIONS
from eie.version import __version__


def universe(inputs: ImpactInputs, kind: ModelType) -> tuple[str, ...]:
    if kind is ModelType.SUPPLIED_FACTOR_MODEL:
        return tuple(f.factor_id for f in inputs.factors)
    if kind is ModelType.MATCHED_CONTROLS:
        return tuple(c.instrument_id for c in inputs.controls)
    return () if inputs.benchmark is None else (inputs.benchmark.identity.instrument_id,)


def calculate(inputs: ImpactInputs, spec: CounterfactualModelSpec) -> EventImpactResult:
    verify_inputs(inputs, spec)
    if spec.engine_version != __version__ or spec.universe != universe(inputs, spec.model_type):
        raise ValidationError("unsupported impact engine or changed model universe")
    if (
        spec.model_type is ModelType.MATCHED_CONTROLS
        and (inputs.benchmark is not None or inputs.factors)
        or spec.model_type is ModelType.SUPPLIED_FACTOR_MODEL
        and (inputs.benchmark is not None or inputs.controls)
        or spec.model_type not in (ModelType.MATCHED_CONTROLS, ModelType.SUPPLIED_FACTOR_MODEL)
        and (inputs.factors or inputs.controls)
    ):
        raise ValidationError("unrelated counterfactual inputs require a separate model")
    fit = fit_model(inputs, spec)
    curve = abnormal_curve(inputs, spec, fit)
    cars = cumulative_returns(inputs, spec, fit, curve)
    quality = quality_flags(inputs, spec, fit)
    quality.update(q for p in curve for q in p.quality)
    if not curve or any(c.value is None for c in cars):
        quality.add(ImpactQuality.MISSING_EVENT_WINDOW_DATA)
    metrics = (
        ()
        if spec.contamination_policy is ContaminationPolicy.EXCLUDE_EVENT
        and inputs.study.contamination
        else control_metrics(inputs, fit)
    )
    times = [inputs.study.available_ns]
    times.extend(p.available_ns for f in inputs.factors for p in f.observations)
    for data in (inputs.target, inputs.benchmark, *(c.market for c in inputs.controls)):
        if data is not None:
            times.extend(b.available_ns for b in data.bars)
            times.extend(q.available_ns for q in data.quotes)
            times.extend(a.available_ns for a in data.splits)
    return EventImpactResult(
        inputs, spec, fit, curve, cars, metrics, frozenset(quality), max(times), ASSUMPTIONS
    )


def control_metrics(inputs: ImpactInputs, fit: ModelFitResult) -> tuple[MetricCounterfactual, ...]:
    from eie.studies.engine import calculate as raw_study

    if not isinstance(fit.payload, ControlFit) or fit.status is not FitStatus.AVAILABLE:
        return ()
    market = {c.instrument_id: c.market for c in inputs.controls}
    samples = []
    for c in fit.payload.selected:
        data = market[c.instrument_id]
        assert data is not None
        r = raw_study(inputs.study.definition, data, (), ())
        samples.append((c.weight, r))
    result = []
    # Comparable-window supplied-control metric expectations, not the basket's realized vol.
    for metric, observed, values in (
        (
            "REALIZED_LOG_VOLATILITY",
            inputs.study.volatility.post,
            tuple(r.volatility.post for _, r in samples),
        ),
        (
            "QUOTED_SPREAD",
            inputs.study.liquidity.post_spread,
            tuple(r.liquidity.post_spread for _, r in samples),
        ),
    ):
        if observed is None or any(v is None for v in values):
            continue
        try:
            expected = fsum(
                w * v for (w, _), v in zip(samples, values, strict=True) if v is not None
            )
            deviation = observed - expected
            if isfinite(expected) and isfinite(deviation):
                result.append(
                    MetricCounterfactual(
                        metric,
                        observed,
                        expected,
                        deviation,
                        "MATCHED_CONTROL_WEIGHTED_COMPARABLE_WINDOW_METRIC",
                        tuple(r.result_fingerprint for _, r in samples),
                    )
                )
        except ArithmeticError:
            continue
    return tuple(result)


class ImpactEngine:
    """Supplied inputs only. Every invocation declares its model before seeing residuals."""

    def impact_event(
        self,
        inputs: ImpactInputs,
        model_type: ModelType,
        estimation_window: EstimationWindow,
        car_windows: tuple[CARWindow, ...],
        *,
        fit_cutoff_ns: int | None = None,
        minimum_observations: int = 30,
        intercept: bool = True,
        matching: MatchingPolicy | None = None,
        model_version: str = "ols-qr-controls-1",
        confidence_level: float = 0.95,
        exclude_contaminated: bool = False,
        exclude_fit_contamination: bool = False,
    ) -> EventImpactResult:
        from eie.impact.models import ContaminationPolicy

        retained = project_inputs(inputs, estimation_window)
        study = retained.study
        method = (
            FitMethod.OLS_QR
            if model_type in (ModelType.MARKET_MODEL, ModelType.SUPPLIED_FACTOR_MODEL)
            else FitMethod.STANDARDIZED_EUCLIDEAN
            if model_type is ModelType.MATCHED_CONTROLS
            else FitMethod.IDENTITY_BENCHMARK
        )
        spec = CounterfactualModelSpec(
            model_type,
            estimation_window,
            study.definition.window,
            car_windows,
            semantics(retained, estimation_window),
            study.definition.event_build_id,
            study.definition.event_dataset_fingerprint,
            study.study_id,
            study.result_fingerprint,
            retained.input_fingerprint,
            dataset_pins(retained),
            universe(retained, model_type),
            method,
            study.anchor_ns if fit_cutoff_ns is None else fit_cutoff_ns,
            minimum_observations=minimum_observations,
            intercept=intercept,
            contamination_policy=ContaminationPolicy.EXCLUDE_EVENT
            if exclude_contaminated
            else ContaminationPolicy.INCLUDE,
            exclude_fit_contamination=exclude_fit_contamination,
            matching=(matching or MatchingPolicy())
            if model_type is ModelType.MATCHED_CONTROLS
            else matching,
            confidence_level=confidence_level,
            model_version=model_version,
        )
        return calculate(retained, spec)

    def impact_events(
        self,
        requests: tuple[
            tuple[ImpactInputs, ModelType, EstimationWindow, tuple[CARWindow, ...]], ...
        ],
    ) -> tuple[EventImpactResult, ...]:
        if not isinstance(requests, tuple) or not 1 <= len(requests) <= 10_000:
            raise ValidationError("bounded immutable impact batch required")
        results = tuple(
            sorted((self.impact_event(*r) for r in requests), key=lambda r: r.impact_id)
        )
        if len({r.impact_id for r in results}) != len(results):
            raise ValidationError("duplicate impact batch request")
        return results


def run_manifest(results: tuple[EventImpactResult, ...]) -> ImpactRunManifest:
    if (
        not results
        or len(
            {(r.model_spec.event_build_id, r.model_spec.event_dataset_fingerprint) for r in results}
        )
        != 1
    ):
        raise ValidationError("impact run requires one compatible event dataset build")
    rows = tuple(sorted(results, key=lambda r: r.impact_id))
    return ImpactRunManifest(
        rows[0].model_spec.event_build_id,
        rows[0].model_spec.event_dataset_fingerprint,
        tuple(r.impact_id for r in rows),
        tuple(r.result_fingerprint for r in rows),
        tuple(r.model_spec.model_id for r in rows),
        tuple(
            sorted(
                {p for r in rows for p in r.model_spec.datasets},
                key=lambda p: (p.role, p.member_id, p.dataset_id, p.fingerprint, p.version),
            )
        ),
        sum(r.fit.status is not FitStatus.AVAILABLE for r in rows),
        sum(ImpactQuality.TARGET_CONTAMINATION in r.quality for r in rows),
    )
