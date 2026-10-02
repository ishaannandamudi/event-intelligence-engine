"""Exact paired prior observations and specialized transparent model fit payloads."""

from eie.core.fingerprints import fingerprint
from eie.impact.controls import select_controls
from eie.impact.estimation import excluded, fitting_periods
from eie.impact.inputs import ImpactInputs
from eie.impact.models import (
    BenchmarkFit,
    CounterfactualModelSpec,
    FitStatus,
    ModelFitResult,
    ModelType,
)
from eie.impact.regression import ols


def fit_model(inputs: ImpactInputs, spec: CounterfactualModelSpec) -> ModelFitResult:
    target = fitting_periods(inputs, inputs.target, spec)
    if spec.model_type is ModelType.MATCHED_CONTROLS:
        status, controls = select_controls(inputs, spec)
        return ModelFitResult(
            status,
            len(controls.selected),
            len(target),
            0,
            len(controls.exclusions),
            (),
            controls,
            () if status is FitStatus.AVAILABLE else (status.value,),
        )
    if spec.model_type is ModelType.SUPPLIED_FACTOR_MODEL:
        names = tuple(f.factor_id for f in inputs.factors)
        maps = tuple(
            {
                (o.start_ns, o.end_ns): (
                    o.value,
                    fingerprint("eie:factor-period:v1", (f.factor_id, o)),
                )
                for o in f.observations
                if o.available_ns < spec.fit_cutoff_ns and o.end_ns < spec.fit_cutoff_ns
            }
            for f in inputs.factors
        )
    else:
        names = () if inputs.benchmark is None else (inputs.benchmark.identity.instrument_id,)
        maps = (
            ()
            if inputs.benchmark is None
            else (
                {
                    (p.start_ns, p.end_ns): (p.value, p.input_fingerprint)
                    for p in fitting_periods(inputs, inputs.benchmark, spec)
                },
            )
        )
    if not names:
        return ModelFitResult(
            FitStatus.MISSING_INPUT,
            0,
            len(target),
            len(target),
            0,
            (),
            None,
            (
                "MISSING_FACTORS"
                if spec.model_type is ModelType.SUPPLIED_FACTOR_MODEL
                else "MISSING_BENCHMARK",
            ),
        )
    ys, xs, fingerprints = [], [], []
    missing = removed = 0
    for p in target:
        if excluded(inputs, spec, p, (inputs.target.identity.instrument_id, *names)):
            removed += 1
            continue
        values = tuple(m.get((p.start_ns, p.end_ns)) for m in maps)
        if p.value is None or any(v is None or v[0] is None for v in values):
            missing += 1
            continue
        xs.append(tuple(v[0] for v in values if v is not None and v[0] is not None))
        ys.append(p.value)
        fingerprints.append(
            fingerprint(
                "eie:impact-fit-pair:v1",
                (p.input_fingerprint, tuple(v[1] for v in values if v is not None)),
            )
        )
    if spec.model_type in (ModelType.MARKET_ADJUSTED, ModelType.SECTOR_ADJUSTED):
        return ModelFitResult(
            FitStatus.AVAILABLE,
            len(ys),
            len(target),
            missing,
            removed,
            tuple(fingerprints),
            BenchmarkFit(names[0]),
            (),
        )
    minimum = (
        max(spec.minimum_observations, 5 * (len(names) + int(spec.intercept)))
        if spec.model_type is ModelType.SUPPLIED_FACTOR_MODEL
        else spec.minimum_observations
    )
    status, payload = ols(
        tuple(ys),
        tuple(xs),
        names,
        intercept=spec.intercept,
        minimum=minimum,
        maximum_condition=spec.maximum_condition,
    )
    return ModelFitResult(
        status,
        len(ys),
        len(target),
        missing,
        removed,
        tuple(fingerprints),
        payload,
        () if status is FitStatus.AVAILABLE else (status.value,),
    )
