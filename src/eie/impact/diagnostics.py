"""Categorical interpretation flags, with no calibrated confidence score."""

from eie.backfill.models import ReconstructionQuality
from eie.impact.inputs import ImpactInputs
from eie.impact.models import (
    ControlFit,
    CounterfactualModelSpec,
    FitStatus,
    ImpactQuality,
    ModelFitResult,
    ModelType,
    RegressionFit,
)
from eie.studies.market import Adjustment
from eie.studies.models import StudyQuality


def quality_flags(
    inputs: ImpactInputs, spec: CounterfactualModelSpec, fit: ModelFitResult
) -> set[ImpactQuality]:
    s = inputs.study
    flags = set()
    if s.contamination:
        flags.add(ImpactQuality.TARGET_CONTAMINATION)
    if StudyQuality.COARSE_EVENT_TIME in s.quality:
        flags.add(ImpactQuality.COARSE_EVENT_TIME)
    if (
        s.definition.event_gap_fingerprints
        or s.definition.reconstruction_quality
        is not ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME
    ):
        flags.add(ImpactQuality.RECONSTRUCTION_LIMITATION)
    if inputs.target.identity.adjustment is Adjustment.UNADJUSTED:
        flags.add(ImpactQuality.CORPORATE_ACTION_RISK)
    if fit.status is FitStatus.INSUFFICIENT:
        flags.add(ImpactQuality.INSUFFICIENT_ESTIMATION_HISTORY)
    if fit.status in (FitStatus.SINGULAR, FitStatus.DEGENERATE):
        flags.add(
            ImpactQuality.SINGULAR_FACTORS
            if spec.model_type is ModelType.SUPPLIED_FACTOR_MODEL
            else ImpactQuality.DEGENERATE_BENCHMARK
        )
    if fit.status is FitStatus.NUMERIC_UNAVAILABLE:
        flags.add(ImpactQuality.NUMERIC_UNAVAILABLE)
    if fit.status is FitStatus.MISSING_INPUT:
        flags.add(
            ImpactQuality.MISSING_FACTORS
            if spec.model_type is ModelType.SUPPLIED_FACTOR_MODEL
            else ImpactQuality.POOR_CONTROL_MATCH
            if spec.model_type is ModelType.MATCHED_CONTROLS
            else ImpactQuality.MISSING_BENCHMARK
        )
    if fit.missing_pair_count or StudyQuality.DATA_GAP in s.quality:
        flags.add(ImpactQuality.DATA_GAP)
    if fit.excluded_count and spec.exclude_fit_contamination:
        flags.add(ImpactQuality.FIT_CONTAMINATION_EXCLUDED)
    p = fit.payload
    if isinstance(p, RegressionFit):
        if p.r_squared is None or p.r_squared < spec.low_r2_threshold:
            flags.add(ImpactQuality.LOW_R2)
        if fit.observation_count < spec.minimum_uncertainty_observations:
            flags.add(ImpactQuality.UNCERTAINTY_UNAVAILABLE)
    if isinstance(p, ControlFit):
        assert spec.matching is not None
        if len(p.selected) < spec.matching.k or any(
            c.distance > spec.matching.maximum_distance for c in p.selected
        ):
            flags.add(ImpactQuality.POOR_CONTROL_MATCH)
        if any(c.contaminated for c in p.selected) or any(
            r == "CONTROL_CONTAMINATION" for _, r in p.exclusions
        ):
            flags.add(ImpactQuality.CONTROL_CONTAMINATION)
    return flags
