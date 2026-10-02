"""Immutable bounded factor, feature, control and retained market inputs."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.studies.checks import contract, digest
from eie.studies.market import Adjustment, MarketSlice, ReturnType, Timeframe
from eie.studies.models import ContaminationFlag, EventStudyResult, ReturnMethod


@dataclass(frozen=True, slots=True)
class ReturnSemantics:
    timeframe: Timeframe
    adjustment: Adjustment
    return_type: ReturnType
    method: ReturnMethod
    session_fingerprint: str
    period_definition: str = "ADJACENT_CLOSE_TO_CLOSE"

    def __post_init__(self) -> None:
        contract(self)
        digest(self.session_fingerprint)
        if self.period_definition != "ADJACENT_CLOSE_TO_CLOSE" or (
            self.return_type is ReturnType.TOTAL_RETURN
            and self.adjustment is not Adjustment.SPLIT_DIVIDEND_ADJUSTED
        ):
            raise ValidationError("invalid impact return semantics")


@dataclass(frozen=True, slots=True)
class FactorObservation:
    start_ns: int
    end_ns: int
    value: float | None
    available_ns: int

    def __post_init__(self) -> None:
        contract(self)
        if not self.start_ns < self.end_ns <= self.available_ns:
            raise ValidationError("factor period must complete before availability")


@dataclass(frozen=True, slots=True)
class FactorSeries:
    factor_id: str
    source: str
    dataset_id: str
    dataset_fingerprint: str
    version: str
    semantics: ReturnSemantics
    observations: tuple[FactorObservation, ...]

    def __post_init__(self) -> None:
        contract(self)
        digest(self.dataset_fingerprint)
        if (
            self.factor_id == "INTERCEPT"
            or len(self.observations) > 100_000
            or any(
                a.end_ns > b.start_ns
                for a, b in zip(self.observations, self.observations[1:], strict=False)
            )
        ):
            raise ValidationError("bounded ordered nonoverlapping factor observations required")


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    instrument_id: str
    timestamp_ns: int
    available_ns: int
    values: tuple[tuple[str, float], ...]
    source: str
    dataset_id: str
    dataset_fingerprint: str
    version: str

    def __post_init__(self) -> None:
        contract(self)
        digest(self.dataset_fingerprint)
        if (
            self.timestamp_ns > self.available_ns
            or not 1 <= len(self.values) <= 16
            or tuple(k for k, _ in self.values) != tuple(sorted(dict(self.values)))
            or any(
                not k or k != k.strip() or len(k) > 256 or any(ord(c) < 32 for c in k)
                for k, _ in self.values
            )
        ):
            raise ValidationError("bounded unique sorted feature names and causal clocks required")


@dataclass(frozen=True, slots=True)
class ControlCandidate:
    instrument_id: str
    market: MarketSlice | None
    features: FeatureSnapshot | None
    contamination: tuple[ContaminationFlag, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        if (
            self.market is not None
            and self.market.identity.instrument_id != self.instrument_id
            or self.features is not None
            and self.features.instrument_id != self.instrument_id
        ):
            raise ValidationError("control market/feature instrument mismatch")
        if self.contamination != tuple(
            sorted(
                set(self.contamination), key=lambda c: (c.timestamp_ns, c.event_id, c.revision_id)
            )
        ):
            raise ValidationError("sorted unique control contamination required")
        if len({(c.timestamp_ns, c.event_id, c.revision_id) for c in self.contamination}) != len(
            self.contamination
        ):
            raise ValidationError("ambiguous control contamination vintages")


@dataclass(frozen=True, slots=True)
class FitExclusion:
    instrument_id: str
    start_ns: int
    end_ns: int
    available_ns: int
    event_id: str
    source: str

    def __post_init__(self) -> None:
        contract(self)
        if self.start_ns >= self.end_ns:
            raise ValidationError("empty fitting exclusion")


@dataclass(frozen=True, slots=True)
class ImpactInputs:
    study: EventStudyResult
    target: MarketSlice
    benchmark: MarketSlice | None = None
    factors: tuple[FactorSeries, ...] = ()
    target_features: FeatureSnapshot | None = None
    controls: tuple[ControlCandidate, ...] = ()
    fit_exclusions: tuple[FitExclusion, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        if self.target.identity != self.study.market.identity:
            raise ValidationError("impact target must pin the raw study dataset")
        if len(self.factors) > 8 or tuple(f.factor_id for f in self.factors) != tuple(
            sorted({f.factor_id for f in self.factors})
        ):
            raise ValidationError("at most eight sorted unique factors required")
        if len(self.controls) > 1000 or tuple(c.instrument_id for c in self.controls) != tuple(
            sorted({c.instrument_id for c in self.controls})
        ):
            raise ValidationError("at most 1000 sorted unique controls required")
        if self.target_features is not None and (
            self.target_features.instrument_id != self.target.identity.instrument_id
        ):
            raise ValidationError("target features belong to another instrument")
        if len(self.fit_exclusions) > 10_000 or self.fit_exclusions != tuple(
            sorted(
                set(self.fit_exclusions),
                key=lambda x: (
                    x.instrument_id,
                    x.start_ns,
                    x.end_ns,
                    x.available_ns,
                    x.event_id,
                    x.source,
                ),
            )
        ):
            raise ValidationError("bounded sorted unique fitting exclusions required")
        size = sum(
            len(s.bars) + len(s.sessions) + len(s.quotes) + len(s.gaps) + len(s.splits)
            for s in (
                self.target,
                *((self.benchmark,) if self.benchmark is not None else ()),
                *(c.market for c in self.controls if c.market is not None),
            )
        ) + sum(len(f.observations) for f in self.factors)
        if size > 200_000:
            raise ValidationError("bounded total impact input observations required")

    @property
    def input_fingerprint(self) -> str:
        return fingerprint("eie:impact-inputs:v1", self)
