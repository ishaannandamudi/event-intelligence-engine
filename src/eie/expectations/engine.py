"""Causal in-memory queries; no cached statistics or wall-clock reads."""

from copy import deepcopy
from dataclasses import replace

from eie.config.settings import Settings
from eie.core.errors import PersistenceError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.corporate.types import EarningsPeriod
from eie.entities.sec import SecEntityRegistry
from eie.events.scope import ScopeKind
from eie.events.taxonomy import EventFamily
from eie.expectations.consensus import (
    AggregationMethod,
    ConsensusOrigin,
    ConsensusSnapshot,
    consensus_snapshot,
)
from eie.expectations.guidance import GuidanceComparison, GuidanceObservation, compare_guidance
from eie.expectations.history import ObservationLedger
from eie.expectations.models import (
    EventReference,
    MeasurementContext,
    MeasurementId,
    Period,
    SeasonalAdjustment,
)
from eie.expectations.normalization import HistoricalSurprise, historical_scale
from eie.expectations.observations import ActualObservation, ActualRole, EstimateObservation
from eie.expectations.queries import EstimateCoverage, Observation, ObservationHistory
from eie.expectations.settings import CutoffPolicy
from eie.expectations.surprise import SurpriseObservation, calculate_surprise, with_normalization
from eie.expectations.units import number
from eie.persistence.memory import InMemoryEventStore
from eie.providers.common.models import ReferencePeriod
from eie.sources.models import SourceHealth, SourceHealthState


class ExpectationsEngine:
    def __init__(
        self,
        settings: Settings,
        event_store: InMemoryEventStore | None = None,
        registry: SecEntityRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.event_store = (
            deepcopy(event_store) if event_store is not None else InMemoryEventStore()
        )
        self.registry = deepcopy(registry) if registry is not None else SecEntityRegistry()
        self.ledger = ObservationLedger()
        self._events: dict[str, EventReference] = {}
        self._coverage: list[EstimateCoverage] = []
        self._health: list[tuple[str, int, SourceHealth]] = []

    def bind_event(self, event_id: str, reference_period: Period, as_of_ns: int) -> EventReference:
        """Bind an exact known event to its supplied, validated reference period."""
        require_ns(as_of_ns)
        event = self.event_store.get_event(event_id, as_of_ns)
        if event is None:
            raise ValidationError("expectation event is unresolved")
        family = event.identity.event_type.family
        if (
            (family is EventFamily.EARNINGS and not isinstance(reference_period, EarningsPeriod))
            or (family is EventFamily.MACRO and not isinstance(reference_period, ReferencePeriod))
            or family not in (EventFamily.EARNINGS, EventFamily.MACRO)
        ):
            raise ValidationError("event family and reference period disagree")
        issuer = security = None
        if (
            event.identity.scope.kind in (ScopeKind.COMPANY, ScopeKind.SECURITY)
            and len(event.identity.scope.identifiers) != 1
        ):
            raise ValidationError("expectations require a single issuer/security event")
        if event.identity.scope.kind is ScopeKind.COMPANY:
            issuer = event.identity.scope.identifiers[0]
            if self.registry.issuer_by_id(issuer, as_of_ns) is None:
                raise ValidationError("expectation issuer is unresolved")
        elif event.identity.scope.kind is ScopeKind.SECURITY:
            security = event.identity.scope.identifiers[0]
            record = self.registry.security_by_id(security)
            if record is None or self.registry.issuer_by_id(record.issuer_id, as_of_ns) is None:
                raise ValidationError("expectation security is unresolved")
            issuer = record.issuer_id
        facts = {v.name: v.value for v in event.facts}
        if isinstance(reference_period, EarningsPeriod):
            if "reference_period" in facts and facts["reference_period"] != reference_period.key:
                raise ValidationError("earnings reference period disagrees with event")
        elif isinstance(reference_period, ReferencePeriod):
            for name, value in (
                ("period_start", reference_period.start_date),
                ("period_end", reference_period.end_date),
                ("period_frequency", reference_period.frequency.value),
                ("period_label", reference_period.label),
            ):
                if name in facts and facts[name] != value:
                    raise ValidationError("macro reference period disagrees with event")
        else:
            raise ValidationError("typed reference period required")
        stage = facts.get("release_stage")
        if stage is not None and not isinstance(stage, str):
            raise ValidationError("event release stage must be text")
        row = EventReference(
            event_id, event.identity.event_type, reference_period, as_of_ns, issuer, security, stage
        )
        prior = self._events.get(event_id)
        if prior is not None:
            if replace(row, available_from_ns=prior.available_from_ns) != prior:
                raise PersistenceError("event reference identity changed")
            return prior
        self._events[event_id] = row
        return row

    def _context(
        self,
        event_id: str,
        measurement: MeasurementId,
        as_of_ns: int,
        reference_period: Period | None = None,
        seasonal_adjustment: SeasonalAdjustment = SeasonalAdjustment.UNSPECIFIED,
    ) -> MeasurementContext:
        require_ns(as_of_ns)
        ref = self._events.get(event_id)
        if ref is None or ref.available_from_ns > as_of_ns:
            raise ValidationError("event reference not known as of query")
        return MeasurementContext(
            event_id,
            measurement,
            reference_period or ref.reference_period,
            ref.release_stage,
            seasonal_adjustment,
        )

    def append(self, observation: Observation) -> bool:
        if not self.settings.expectations.enabled:
            raise ValidationError("expectations ingestion is disabled")
        context = observation.context
        ref = self._events.get(context.event_id)
        if ref is None or ref.available_from_ns > observation.source.available_from_ns:
            raise ValidationError("expectation event reference is unresolved")
        family = context.measurement_id.code.split(".")[0]
        if (family == "MACRO" and ref.event_type.family is not EventFamily.MACRO) or (
            family in ("EARNINGS", "GUIDANCE") and ref.event_type.family is not EventFamily.EARNINGS
        ):
            raise ValidationError("measurement and event families disagree")
        if family != "GUIDANCE" and context.reference_period != ref.reference_period:
            raise ValidationError("estimate/actual period disagrees with event")
        if context.release_stage != ref.release_stage:
            raise ValidationError("measurement release stage disagrees with event")
        if isinstance(observation, GuidanceObservation) and observation.issuer_id != ref.issuer_id:
            raise ValidationError("guidance issuer disagrees with event")
        return self.ledger.append(observation, self.event_store)

    def append_coverage(self, coverage: EstimateCoverage) -> None:
        if coverage not in self._coverage:
            self._coverage.append(coverage)

    def record_health(self, provider_id: str, available_ns: int, health: SourceHealth) -> None:
        require_ns(available_ns)
        if health.last_attempt_ns != available_ns:
            raise ValidationError("health knowledge disagrees with attempt")
        self._health.append((provider_id, available_ns, health))

    def health_as_of(self, provider_id: str, as_of_ns: int) -> SourceHealth:
        require_ns(as_of_ns)
        rows = tuple(v for v in self._health if v[0] == provider_id and v[1] <= as_of_ns)
        if not rows:
            return SourceHealth(SourceHealthState.UNKNOWN)
        health = max(rows, key=lambda v: v[1])[2]
        if (
            health.state is SourceHealthState.HEALTHY
            and health.last_success_ns is not None
            and as_of_ns - health.last_success_ns > self.settings.expectations.staleness_ns
        ):
            return replace(health, state=SourceHealthState.STALE)
        return health

    def _history(
        self,
        event_id: str,
        measurement: MeasurementId,
        as_of_ns: int,
        observation_type: type[Observation],
    ) -> ObservationHistory:
        require_ns(as_of_ns)
        rows = tuple(
            v
            for v in self.ledger.all()
            if isinstance(v, observation_type)
            and v.context.event_id == event_id
            and v.context.measurement_id == measurement
            and v.source.available_from_ns <= as_of_ns
        )
        return ObservationHistory(as_of_ns, rows, self.settings.fingerprint)

    def estimates(
        self, event_id: str, measurement: MeasurementId, as_of_ns: int
    ) -> ObservationHistory:
        return self._history(event_id, measurement, as_of_ns, EstimateObservation)

    def actuals(
        self, event_id: str, measurement: MeasurementId, as_of_ns: int
    ) -> ObservationHistory:
        return self._history(event_id, measurement, as_of_ns, ActualObservation)

    def guidance(
        self, event_id: str, measurement: MeasurementId, as_of_ns: int
    ) -> ObservationHistory:
        return self._history(event_id, measurement, as_of_ns, GuidanceObservation)

    @staticmethod
    def _latest(rows: tuple[Observation, ...]) -> tuple[Observation, ...]:
        latest = {v.stream_id: v for v in rows}
        return tuple(latest[key] for key in sorted(latest))

    def consensus(
        self,
        event_id: str,
        measurement: MeasurementId,
        as_of_ns: int,
        *,
        provider_id: str | None = None,
        method: AggregationMethod = AggregationMethod.MEAN,
        origin: ConsensusOrigin = ConsensusOrigin.PROVIDER,
        reference_period: Period | None = None,
        seasonal_adjustment: SeasonalAdjustment = SeasonalAdjustment.UNSPECIFIED,
    ) -> ConsensusSnapshot:
        context = self._context(
            event_id, measurement, as_of_ns, reference_period, seasonal_adjustment
        )
        return self._consensus_context(context, as_of_ns, provider_id, method, origin)

    def _consensus_context(
        self,
        context: MeasurementContext,
        as_of_ns: int,
        provider_id: str | None,
        method: AggregationMethod,
        origin: ConsensusOrigin,
        *,
        exclude_all: bool = False,
    ) -> ConsensusSnapshot:
        event_id, measurement = context.event_id, context.measurement_id
        rows = tuple(
            v
            for v in self._latest(self.estimates(event_id, measurement, as_of_ns).observations)
            if isinstance(v, EstimateObservation)
        )
        if exclude_all:
            rows = ()
        coverage = tuple(
            sorted(
                (
                    v
                    for v in self._coverage
                    if v.available_from_ns <= as_of_ns
                    and (provider_id is None or v.provider_id == provider_id)
                ),
                key=lambda v: (v.provider_id, v.available_from_ns, v.event_ids),
            )
        )
        providers = sorted(
            {v[0] for v in self._health if v[1] <= as_of_ns} | {v.source.provider_id for v in rows}
        )
        health = tuple(
            (p, self.health_as_of(p, as_of_ns))
            for p in providers
            if provider_id is None or p == provider_id
        )
        return consensus_snapshot(
            context,
            rows,
            as_of_ns,
            self.settings.expectations,
            self.settings.source_precedence,
            self.settings.fingerprint,
            coverage,
            health,
            provider_id=provider_id,
            method=method,
            origin=origin,
        )

    def _raw_surprise(
        self,
        context: MeasurementContext,
        as_of_ns: int,
        provider_id: str | None,
        method: AggregationMethod,
        origin: ConsensusOrigin,
    ) -> SurpriseObservation:
        history = self.actuals(context.event_id, context.measurement_id, as_of_ns).observations
        rows = tuple(
            v
            for v in self._latest(history)
            if isinstance(v, ActualObservation) and v.context == context
        )
        actuals = tuple(v for v in rows if v.role is ActualRole.ACTUAL)
        previous = tuple(v for v in rows if v.role is ActualRole.PREVIOUS)
        revised = tuple(v for v in rows if v.role is ActualRole.REVISED_PREVIOUS)
        order = self.settings.source_precedence.order_for(
            f"actual.{context.measurement_id.code.lower()}"
        )
        actual = (
            min(
                actuals,
                key=lambda v: (
                    order.index(v.source.source_class),
                    -v.source.available_from_ns,
                    v.stream_id,
                ),
            )
            if actuals
            else None
        )
        originals = tuple(
            v
            for v in history
            if isinstance(v, ActualObservation)
            and v.context == context
            and v.role is ActualRole.ACTUAL
            and v.revision.number == 1
        )
        release = min((v.published_at_ns for v in originals), default=None)
        available = min((v.source.available_from_ns for v in originals), default=None)
        cutoff = (
            None
            if release is None
            else min(release, available if available is not None else release)
        )
        consensus_asof = (
            as_of_ns
            if cutoff is None
            else max(
                0,
                cutoff
                - int(
                    self.settings.expectations.consensus_cutoff_policy is CutoffPolicy.STRICT_BEFORE
                ),
            )
        )
        snapshot = self._consensus_context(
            context,
            consensus_asof,
            provider_id,
            method,
            origin,
            exclude_all=cutoff == 0
            and self.settings.expectations.consensus_cutoff_policy is CutoffPolicy.STRICT_BEFORE,
        )
        # Even inclusive availability policy cannot admit a provider vintage after publication.
        if cutoff is not None and any(
            v.source.vintage_at_ns is not None and v.source.vintage_at_ns > cutoff
            for v in snapshot.inputs
        ):
            raise ValidationError("post-release provider vintage in consensus")
        return calculate_surprise(
            context,
            as_of_ns,
            actual,
            snapshot,
            self.settings.expectations,
            self.settings.fingerprint,
            claims=actuals,
            previous=previous,
            revised_previous=revised,
            cutoff_ns=cutoff,
            release_at_ns=release,
            original_available_ns=available,
        )

    def surprise(
        self,
        event_id: str,
        measurement: MeasurementId,
        as_of_ns: int,
        *,
        provider_id: str | None = None,
        method: AggregationMethod = AggregationMethod.MEAN,
        origin: ConsensusOrigin = ConsensusOrigin.PROVIDER,
        seasonal_adjustment: SeasonalAdjustment = SeasonalAdjustment.UNSPECIFIED,
    ) -> SurpriseObservation:
        context = self._context(
            event_id, measurement, as_of_ns, seasonal_adjustment=seasonal_adjustment
        )
        result = self._raw_surprise(context, as_of_ns, provider_id, method, origin)
        if (
            result.raw_surprise is None
            or result.actual is None
            or result.consensus_cutoff_ns is None
            or result.consensus.provider_id is None
        ):
            return result
        ref = self._events[event_id]
        history = self.surprise_history(
            measurement,
            max(0, result.consensus_cutoff_ns - 1),
            provider_id=provider_id,
            method=method,
            origin=origin,
            seasonal_adjustment=seasonal_adjustment,
        )
        scale = historical_scale(
            context,
            history,
            result.consensus_cutoff_ns,
            result.actual.value.canonical_unit,
            ref.event_type.code,
            ref.issuer_id,
            result.consensus.provider_id,
            method,
            self.settings.expectations,
            self.settings.fingerprint,
        )
        return with_normalization(result, scale)

    def surprise_history(
        self,
        measurement: MeasurementId,
        as_of_ns: int,
        *,
        provider_id: str | None = None,
        method: AggregationMethod = AggregationMethod.MEAN,
        origin: ConsensusOrigin = ConsensusOrigin.PROVIDER,
        seasonal_adjustment: SeasonalAdjustment = SeasonalAdjustment.UNSPECIFIED,
    ) -> tuple[HistoricalSurprise, ...]:
        require_ns(as_of_ns)
        history = []
        for event_id in sorted(self._events):
            ref = self._events[event_id]
            if ref.available_from_ns > as_of_ns:
                continue
            context = self._context(
                event_id, measurement, as_of_ns, seasonal_adjustment=seasonal_adjustment
            )
            result = self._raw_surprise(context, as_of_ns, provider_id, method, origin)
            if (
                result.raw_surprise is not None
                and result.actual is not None
                and result.release_at_ns is not None
                and result.consensus.provider_id is not None
            ):
                history.append(
                    HistoricalSurprise(
                        context,
                        ref.event_type.code,
                        ref.issuer_id,
                        result.actual.value.canonical_unit,
                        result.release_at_ns,
                        result.actual.source.available_from_ns,
                        result.consensus.provider_id,
                        method,
                        result.raw_surprise,
                        result.result_fingerprint,
                    )
                )
        return tuple(sorted(history, key=lambda v: (v.release_at_ns, v.context.event_id)))

    def guidance_change(self, new: GuidanceObservation) -> GuidanceComparison:
        prior = tuple(
            v
            for v in self.ledger.all()
            if isinstance(v, GuidanceObservation)
            and v.issuer_id == new.issuer_id
            and v.context.measurement_id == new.context.measurement_id
            and v.context.reference_period == new.context.reference_period
            and v.context.release_stage == new.context.release_stage
            and v.context.seasonal_adjustment == new.context.seasonal_adjustment
            and v.source.provider_id == new.source.provider_id
            and v.source.available_from_ns < new.source.available_from_ns
        )
        old = max(prior, key=lambda v: (v.source.available_from_ns, v.stream_id)) if prior else None
        return compare_guidance(new, old)

    def guidance_vs_consensus(
        self, new: GuidanceObservation, provider_id: str | None = None
    ) -> GuidanceComparison:
        snapshot = self._consensus_context(
            new.context,
            max(0, new.published_at_ns - 1),
            provider_id,
            AggregationMethod.MEAN,
            ConsensusOrigin.PROVIDER,
            exclude_all=new.published_at_ns == 0,
        )
        reasons: tuple[str, ...] = ()
        if snapshot.value is None or not snapshot.point_in_time:
            reasons = ("CONSENSUS_MISSING_OR_NON_POINT_IN_TIME",)
        elif snapshot.unit != new.unit:
            reasons = ("INCOMPATIBLE_UNITS",)
        elif new.midpoint is None:
            reasons = ("GUIDANCE_MIDPOINT_MISSING",)
        change = None
        if not reasons:
            assert new.midpoint is not None and snapshot.value is not None
            try:
                change = new.midpoint - snapshot.value
                number(change)
            except (ArithmeticError, ValidationError):
                change = None
                reasons = ("NUMERIC_OVERFLOW",)
        return GuidanceComparison(
            "MARKET_CONSENSUS", new, None, snapshot.result_fingerprint, change, None, None, reasons
        )

    def canonical_state_bytes(self) -> bytes:
        coverage = sorted(
            self._coverage, key=lambda v: (v.provider_id, v.available_from_ns, v.event_ids)
        )
        return canonical_json_bytes(
            {
                "events": [self._events[k] for k in sorted(self._events)],
                "observations": self.ledger.all(),
                "coverage": coverage,
                "raw_events": self.event_store.canonical_state_bytes().decode(),
                "registry": self.registry.canonical_state_bytes().decode(),
            }
        )
