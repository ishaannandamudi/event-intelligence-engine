"""Versioned result policies, separate from fail-closed operational bounds."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.events.taxonomy import EventFamily
from eie.evidence.claims import ordered
from eie.evidence.models import ResolutionPolicy
from eie.intelligence.schema import Importance
from eie.studies.checks import contract

FAMILY_IMPORTANCE = tuple(
    sorted(
        (
            f,
            Importance.HIGH
            if f
            in (
                EventFamily.CENTRAL_BANK,
                EventFamily.MACRO,
                EventFamily.EARNINGS,
                EventFamily.REGULATORY_LEGAL,
            )
            else Importance.MODERATE,
        )
        for f in EventFamily
    )
)


@dataclass(frozen=True, slots=True)
class NoveltyPolicy:
    version: str = "prior-information-1"
    historical_samples: int = 256
    minimum_numeric_samples: int = 3
    near_zero: float = 1e-9
    resolution: ResolutionPolicy = ResolutionPolicy()

    def __post_init__(self) -> None:
        contract(self)
        if not 1 <= self.minimum_numeric_samples <= self.historical_samples <= 10000 or (
            self.near_zero <= 0
        ):
            raise ValidationError("bounded numeric baseline policy required")


@dataclass(frozen=True, slots=True)
class ImportancePolicy:
    version: str = "taxonomy-importance-1"
    families: tuple[tuple[EventFamily, Importance], ...] = FAMILY_IMPORTANCE
    event_types: tuple[tuple[str, Importance], ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        if tuple(f for f, _ in self.families) != tuple(sorted(EventFamily)):
            raise ValidationError("static importance must cover every event family")
        ordered(tuple(k for k, _ in self.event_types), 1000)


@dataclass(frozen=True, slots=True)
class MaterialityPolicy:
    version: str = "descriptive-materiality-1"
    currency_thresholds: tuple[tuple[str, tuple[float, float, float]], ...] = (
        ("USD", (10_000_000.0, 100_000_000.0, 1_000_000_000.0)),
    )
    ratio_thresholds: tuple[float, float, float] = (0.01, 0.05, 0.2)
    guidance_thresholds: tuple[float, float, float] = (0.01, 0.05, 0.2)
    exposure_thresholds: tuple[float, float, float] = (0.05, 0.2, 0.5)
    scale_measurements: tuple[str, ...] = ("EARNINGS.ASSETS", "EARNINGS.REVENUE")
    combination_rule: str = "MAX_AVAILABLE_FINANCIAL_SCALE_GUIDANCE_OR_EXPOSURE"

    def __post_init__(self) -> None:
        contract(self)
        ordered(tuple(c for c, _ in self.currency_thresholds), 32)
        ordered(self.scale_measurements, 32, empty=False)
        for thresholds in (
            self.ratio_thresholds,
            self.guidance_thresholds,
            self.exposure_thresholds,
            *(v for _, v in self.currency_thresholds),
        ):
            if thresholds != tuple(sorted(set(thresholds))) or thresholds[0] <= 0:
                raise ValidationError("positive increasing materiality thresholds required")
        if self.combination_rule != "MAX_AVAILABLE_FINANCIAL_SCALE_GUIDANCE_OR_EXPOSURE":
            raise ValidationError("unsupported materiality combination policy")


@dataclass(frozen=True, slots=True)
class IntelligencePolicy:
    version: str = "event-intelligence-1"
    novelty: NoveltyPolicy = NoveltyPolicy()
    importance: ImportancePolicy = ImportancePolicy()
    materiality: MaterialityPolicy = MaterialityPolicy()

    def __post_init__(self) -> None:
        contract(self)

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:intelligence-policy:v1", self)

    @property
    def policy_id(self) -> str:
        return "intel_policy_" + self.fingerprint


@dataclass(frozen=True, slots=True)
class IntelligenceLimits:
    prior_claims: int = 100000
    current_claims: int = 1000
    source_alternatives: int = 256
    extraction_runs: int = 10000
    graph_edges: int = 1000
    components: int = 512
    uncertainty_reasons: int = 64
    query_size: int = 10000

    def __post_init__(self) -> None:
        contract(self)
        if not all(
            1 <= v <= maximum
            for v, maximum in (
                (self.prior_claims, 100000),
                (self.current_claims, 10000),
                (self.source_alternatives, 256),
                (self.extraction_runs, 100000),
                (self.graph_edges, 10000),
                (self.components, 512),
                (self.uncertainty_reasons, 64),
                (self.query_size, 100000),
            )
        ):
            raise ValidationError("bounded intelligence operations required")


DEFAULT_INTELLIGENCE_LIMITS = IntelligenceLimits()
