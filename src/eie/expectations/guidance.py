"""Guidance ranges and distinct company-revision/market-consensus comparisons."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.schema import EXPECTATIONS_SCHEMA_VERSION
from eie.events.revisions import EventRevision
from eie.expectations.models import MeasurementContext, ObservationSource
from eie.expectations.observations import validate_observation
from eie.expectations.units import CanonicalUnit, Number, Quantity, number


@dataclass(frozen=True, slots=True)
class GuidanceObservation:
    context: MeasurementContext
    issuer_id: str
    source: ObservationSource
    revision: EventRevision
    published_at_ns: int
    low: Quantity | None = None
    high: Quantity | None = None
    point: Quantity | None = None
    quality_flags: frozenset[QualityFlag] = frozenset()
    schema_version: str = EXPECTATIONS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        from eie.entities.models import ISSUER_ID

        validate_observation(
            self.context, self.source, self.revision, self.quality_flags, self.schema_version
        )
        from eie.core.time import require_ns

        require_ns(self.published_at_ns)
        if self.published_at_ns > self.source.provider_observed_at_ns:
            raise ValidationError("guidance publication follows observation")
        if (
            not self.context.measurement_id.code.startswith("GUIDANCE.")
            or not isinstance(self.issuer_id, str)
            or not ISSUER_ID.fullmatch(self.issuer_id)
        ):
            raise ValidationError("guidance requires guidance measurement and stable issuer")
        supplied = tuple(v for v in (self.low, self.high, self.point) if v is not None)
        if not supplied or any(not isinstance(v, Quantity) for v in supplied):
            raise ValidationError("guidance requires typed range or point")
        if len({v.canonical_unit for v in supplied}) != 1:
            raise ValidationError("guidance range units disagree")
        if (
            self.low is not None
            and self.high is not None
            and self.low.canonical_value > self.high.canonical_value
        ):
            raise ValidationError("guidance range reversed")
        if self.point is not None and (
            (self.low is not None and self.point.canonical_value < self.low.canonical_value)
            or (self.high is not None and self.point.canonical_value > self.high.canonical_value)
        ):
            raise ValidationError("guidance point outside range")

    @property
    def unit(self) -> CanonicalUnit:
        value = self.point or self.low or self.high
        assert value is not None
        return value.canonical_unit

    @property
    def midpoint(self) -> Number | None:
        if self.low is not None and self.high is not None:
            value = self.low.canonical_value / 2 + self.high.canonical_value / 2
            number(value)
            return value
        return None if self.point is None else self.point.canonical_value

    @property
    def stream_id(self) -> str:
        return "gui_" + fingerprint(
            "eie:guidance-stream:v1",
            (
                self.context,
                self.issuer_id,
                self.source.provider_id,
                self.source.provider_record_id,
            ),
        )

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:guidance-result:v1", self)


@dataclass(frozen=True, slots=True)
class GuidanceComparison:
    comparison: str
    new: GuidanceObservation
    prior: GuidanceObservation | None
    consensus_fingerprint: str | None
    midpoint_change: Number | None
    low_change: Number | None
    high_change: Number | None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.midpoint_change, self.low_change, self.high_change):
            if value is not None:
                number(value)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:guidance-comparison:v1", self)


def compare_guidance(
    new: GuidanceObservation, prior: GuidanceObservation | None
) -> GuidanceComparison:
    reasons: tuple[str, ...] = ()
    if prior is None:
        reasons = ("PRIOR_GUIDANCE_MISSING",)
    elif new.source.available_from_ns <= prior.source.available_from_ns:
        reasons = ("PRIOR_GUIDANCE_NOT_PRIOR",)
    elif (
        new.issuer_id,
        new.context.measurement_id,
        new.context.reference_period,
        new.context.release_stage,
        new.context.seasonal_adjustment,
    ) != (
        prior.issuer_id,
        prior.context.measurement_id,
        prior.context.reference_period,
        prior.context.release_stage,
        prior.context.seasonal_adjustment,
    ):
        reasons = ("GUIDANCE_CONTEXT_MISMATCH",)
    elif new.unit != prior.unit:
        reasons = ("INCOMPATIBLE_UNITS",)
    if reasons:
        return GuidanceComparison(
            "PRIOR_COMPANY_GUIDANCE", new, prior, None, None, None, None, reasons
        )
    assert prior is not None

    def delta(a: Quantity | None, b: Quantity | None) -> Number | None:
        return None if a is None or b is None else a.canonical_value - b.canonical_value

    midpoint = (
        None if new.midpoint is None or prior.midpoint is None else new.midpoint - prior.midpoint
    )
    try:
        return GuidanceComparison(
            "PRIOR_COMPANY_GUIDANCE",
            new,
            prior,
            None,
            midpoint,
            delta(new.low, prior.low),
            delta(new.high, prior.high),
        )
    except (ArithmeticError, ValidationError):
        return GuidanceComparison(
            "PRIOR_COMPANY_GUIDANCE",
            new,
            prior,
            None,
            None,
            None,
            None,
            ("NUMERIC_OVERFLOW",),
        )
