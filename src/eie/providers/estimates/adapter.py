"""Capability checks, source health, raw-first staged batch ingestion."""

import re
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum

from eie.core.errors import EIEError, PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.quality import QualityFlag
from eie.core.time import require_ns
from eie.events.revisions import EventRevision, RevisionKind
from eie.expectations.engine import ExpectationsEngine
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import MeasurementId, ObservationSource
from eie.expectations.observations import ActualObservation, EstimateKind, EstimateObservation
from eie.expectations.queries import EstimateCoverage, Observation
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.estimates.models import (
    EstimateBatch,
    EstimateCapability,
    EstimateRecord,
    RecordKind,
)
from eie.providers.estimates.parser import EXTRACT_VERSION, parse_batch
from eie.sources.models import RawSourceObservation, SourceHealth, SourceHealthState, SourceIdentity
from eie.sources.trust import SourceClass


class EstimateFailure(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    PARSE_ERROR = "PARSE_ERROR"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"


@dataclass(frozen=True, slots=True)
class EstimateDiagnostics:
    provider_id: str
    as_of_ns: int
    items_seen: int
    raw_created: int
    observations_created: int
    duplicates_suppressed: int
    health: SourceHealth
    error_category: EstimateFailure | None = None


class EstimateAdapter:
    def __init__(
        self,
        provider_id: str,
        source_class: SourceClass,
        capabilities: frozenset[EstimateCapability],
    ) -> None:
        if (
            not isinstance(provider_id, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}", provider_id) is None
            or not isinstance(source_class, SourceClass)
            or not isinstance(capabilities, frozenset)
            or any(not isinstance(v, EstimateCapability) for v in capabilities)
        ):
            raise ValidationError("typed estimate adapter identity/capabilities required")
        self.provider_id, self.source_class, self._capabilities = (
            provider_id,
            source_class,
            capabilities,
        )

    def capabilities(self) -> frozenset[EstimateCapability]:
        return self._capabilities

    def parse(self, payload: bytes) -> EstimateBatch:
        batch = parse_batch(payload, self.provider_id)
        for item in batch.items:
            for row in item.records:
                capability = (
                    (
                        EstimateCapability.ANALYST
                        if row.estimate_type is EstimateKind.INDIVIDUAL_ANALYST
                        else EstimateCapability.CONSENSUS
                    )
                    if row.kind is RecordKind.ESTIMATE
                    else EstimateCapability(row.kind.value)
                )
                if capability not in self._capabilities or (
                    row.vintage_at_ns is not None
                    and EstimateCapability.VINTAGES not in self._capabilities
                ):
                    raise ProviderSchemaMismatchError("estimate extract exceeds capabilities")
        if batch.coverage_has_vintages and EstimateCapability.VINTAGES not in self._capabilities:
            raise ProviderSchemaMismatchError("provider cannot assert vintage coverage")
        return batch


def normalized_observation(
    record: EstimateRecord, source: ObservationSource, revision: EventRevision
) -> Observation:
    quality = (
        frozenset({QualityFlag.MISSING_TIMESTAMP})
        if record.kind is RecordKind.ESTIMATE and source.vintage_at_ns is None
        else frozenset()
    )
    if revision.number > 1:
        quality = quality | {QualityFlag.CORRECTED}
    if record.kind is RecordKind.ESTIMATE:
        if record.value is None or record.estimate_type is None:
            raise ValidationError("estimate value/type missing")
        return EstimateObservation(
            record.context,
            source,
            record.value,
            record.estimate_type,
            revision,
            record.contributor_id,
            record.contributor_count,
            quality,
        )
    if record.kind is RecordKind.ACTUAL:
        if record.value is None or record.role is None or record.published_at_ns is None:
            raise ValidationError("actual fields missing")
        return ActualObservation(
            record.context,
            source,
            record.value,
            record.role,
            record.published_at_ns,
            revision,
            record.previous_reference_period,
            quality,
        )
    if record.issuer_id is None or record.published_at_ns is None:
        raise ValidationError("guidance fields missing")
    return GuidanceObservation(
        record.context,
        record.issuer_id,
        source,
        revision,
        record.published_at_ns,
        record.low,
        record.high,
        record.point,
        quality,
    )


class EstimateIngestor:
    def __init__(self, engine: ExpectationsEngine) -> None:
        self._engine = engine
        self._diagnostics: list[EstimateDiagnostics] = []
        self._record_ids: dict[tuple[str, str], str] = {}
        self._attempts: dict[str, int] = {}

    @property
    def engine(self) -> ExpectationsEngine:
        return self._engine

    def diagnostics(self, provider_id: str) -> tuple[EstimateDiagnostics, ...]:
        return tuple(v for v in self._diagnostics if v.provider_id == provider_id)

    def _attempt(self, provider_id: str, at_ns: int) -> None:
        require_ns(at_ns)
        if not self.engine.settings.expectations.enabled:
            raise ValidationError("estimate ingestion disabled")
        previous = self._attempts.get(provider_id)
        if previous is not None and at_ns <= previous:
            raise TemporalConsistencyError("provider attempts must advance")

    def _record(self, diagnostic: EstimateDiagnostics) -> EstimateDiagnostics:
        self._diagnostics.append(diagnostic)
        self._attempts[diagnostic.provider_id] = diagnostic.as_of_ns
        self.engine.record_health(diagnostic.provider_id, diagnostic.as_of_ns, diagnostic.health)
        return diagnostic

    def report_failure(
        self, provider_id: str, as_of_ns: int, category: EstimateFailure
    ) -> EstimateDiagnostics:
        self._attempt(provider_id, as_of_ns)
        return self._failure(provider_id, as_of_ns, category)

    def _failure(
        self, provider_id: str, at_ns: int, category: EstimateFailure
    ) -> EstimateDiagnostics:
        old = self.engine.health_as_of(provider_id, at_ns)
        state = {
            EstimateFailure.UNAVAILABLE: SourceHealthState.UNAVAILABLE,
            EstimateFailure.RATE_LIMITED: SourceHealthState.RATE_LIMITED,
            EstimateFailure.SCHEMA_MISMATCH: SourceHealthState.SCHEMA_MISMATCH,
            EstimateFailure.PARSE_ERROR: SourceHealthState.DEGRADED,
            EstimateFailure.NORMALIZATION_ERROR: SourceHealthState.DEGRADED,
        }[category]
        health = SourceHealth(
            state,
            old.last_success_ns,
            at_ns,
            parse_failures=old.parse_failures
            + int(category in (EstimateFailure.PARSE_ERROR, EstimateFailure.SCHEMA_MISMATCH)),
            error_category=category.value,
        )
        return self._record(EstimateDiagnostics(provider_id, at_ns, 0, 0, 0, 0, health, category))

    def ingest_payload(
        self, adapter: EstimateAdapter, payload: bytes, observed_ns: int, ingested_ns: int
    ) -> EstimateDiagnostics:
        self._attempt(adapter.provider_id, ingested_ns)
        require_ns(observed_ns)
        if observed_ns > ingested_ns:
            raise TemporalConsistencyError("estimate observation follows ingestion")
        settings = self.engine.settings.expectations
        if len(payload) > settings.max_payload_bytes:
            raise ValidationError("estimate payload exceeds maximum")
        if adapter.source_class not in self.engine.settings.accepted_source_classes:
            raise ValidationError("estimate source class is disabled")
        try:
            batch = adapter.parse(payload)
        except ProviderSchemaMismatchError:
            return self._failure(adapter.provider_id, ingested_ns, EstimateFailure.SCHEMA_MISMATCH)
        except ProviderParseError:
            return self._failure(adapter.provider_id, ingested_ns, EstimateFailure.PARSE_ERROR)
        except EIEError:
            return self._failure(adapter.provider_id, ingested_ns, EstimateFailure.SCHEMA_MISMATCH)
        if len(batch.items) > settings.max_items_per_batch:
            raise ValidationError("estimate batch exceeds maximum")
        staged = deepcopy(self.engine)
        record_ids = self._record_ids.copy()
        created = raws = duplicates = 0
        try:
            for item in batch.items:
                raw = RawSourceObservation(
                    SourceIdentity(
                        adapter.provider_id,
                        batch.source_version,
                        "estimate-extract-1",
                        adapter.source_class,
                    ),
                    item.item_id,
                    item.item_version,
                    item.payload_hash,
                    "application/json",
                    observed_ns,
                    ingested_ns,
                    item.published_at_ns,
                    item.source_reference,
                    "STRUCTURED_EXPECTATIONS",
                    tuple(sorted({v.context.event_id for v in item.records})),
                    (("estimate_schema", EXTRACT_VERSION),),
                )
                existing_raw = staged.event_store.get_observation(raw.observation_id)
                if existing_raw is None:
                    staged.event_store.append_observation(raw)
                    raws += 1
                else:
                    raw = existing_raw
                seen: dict[str, Observation] = {}
                for record in item.records:
                    source = ObservationSource(
                        adapter.provider_id,
                        item.item_id,
                        raw.observation_id,
                        adapter.source_class,
                        ingested_ns,
                        ingested_ns,
                        observed_ns,
                        record.vintage_at_ns,
                        record.record_id,
                    )
                    initial = normalized_observation(
                        record, source, EventRevision(1, RevisionKind.ORIGINAL, ingested_ns)
                    )
                    key = (adapter.provider_id, record.record_id)
                    if key in record_ids and record_ids[key] != initial.stream_id:
                        raise PersistenceError("provider estimate record changed identity")
                    record_ids[key] = initial.stream_id
                    if initial.stream_id in seen:
                        if initial != seen[initial.stream_id]:
                            raise PersistenceError("conflicting same-item estimate record")
                        duplicates += 1
                        continue
                    seen[initial.stream_id] = initial
                    history = staged.ledger.history(initial.stream_id)
                    if any(v.source.source_observation_id == raw.observation_id for v in history):
                        duplicates += 1
                        continue
                    if history:
                        revision = EventRevision(
                            len(history) + 1, RevisionKind.CORRECTION, ingested_ns, len(history)
                        )
                        initial = replace(
                            initial,
                            revision=revision,
                            quality_flags=initial.quality_flags | {QualityFlag.CORRECTED},
                        )
                    staged.append(initial)
                    created += 1
            if batch.coverage_has_vintages is not None:
                staged.append_coverage(
                    EstimateCoverage(
                        adapter.provider_id,
                        batch.coverage_event_ids,
                        tuple(MeasurementId(v) for v in batch.coverage_measurements),
                        batch.coverage_has_vintages,
                        ingested_ns,
                    )
                )
        except EIEError:
            return self._failure(
                adapter.provider_id, ingested_ns, EstimateFailure.NORMALIZATION_ERROR
            )
        self._engine = staged
        self._record_ids = record_ids
        old = self.engine.health_as_of(adapter.provider_id, ingested_ns)
        health = SourceHealth(
            SourceHealthState.HEALTHY, ingested_ns, ingested_ns, parse_failures=old.parse_failures
        )
        return self._record(
            EstimateDiagnostics(
                adapter.provider_id,
                ingested_ns,
                len(batch.items),
                raws,
                created,
                duplicates,
                health,
            )
        )
