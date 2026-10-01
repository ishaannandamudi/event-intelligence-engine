"""Explicit-clock ingestion of official extracts into EIE evidence and event stores."""

from dataclasses import dataclass, replace
from enum import StrEnum

from eie.config.settings import Settings
from eie.core.errors import PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.quality import ConfirmationState, QualityFlag
from eie.core.time import require_ns
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent, StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.providers.common.errors import (
    ProviderParseError,
    ProviderRateLimitedError,
    ProviderSchemaMismatchError,
    ProviderTransportError,
)
from eie.providers.common.models import ItemKind, OfficialItem, TimePrecision, validate_source_url
from eie.providers.common.protocols import HttpTransport, OfficialAdapter
from eie.scheduled.coverage import CoverageRecord
from eie.scheduled.index import ScheduledEventIndex
from eie.scheduled.models import ScheduleClaim, ScheduleRevision
from eie.sources.models import RawSourceObservation, SourceHealth, SourceHealthState, SourceIdentity
from eie.sources.trust import SourceClass


class FailureCategory(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    PARSE_ERROR = "PARSE_ERROR"


@dataclass(frozen=True, slots=True)
class IngestionDiagnostics:
    provider_id: str
    as_of_ns: int
    items_seen: int
    raw_created: int
    canonical_created: int
    schedule_revisions_created: int
    duplicates_suppressed: int
    health: SourceHealth
    error_category: FailureCategory | None = None

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if (
            not isinstance(self.provider_id, str)
            or not self.provider_id
            or not isinstance(self.health, SourceHealth)
        ):
            raise ValidationError("typed provider diagnostic required")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.items_seen,
                self.raw_created,
                self.canonical_created,
                self.schedule_revisions_created,
                self.duplicates_suppressed,
            )
        ):
            raise ValidationError("ingestion counters must be nonnegative integers")


class OfficialSourceIngestor:
    """No fetching or clock lookup; callers supply exact observed and ingested times."""

    def __init__(self, index: ScheduledEventIndex) -> None:
        self.index = index
        self.settings: Settings = index.settings
        self._diagnostics: dict[str, list[IngestionDiagnostics]] = {}

    def diagnostics(self, provider_id: str) -> tuple[IngestionDiagnostics, ...]:
        return tuple(self._diagnostics.get(provider_id, ()))

    @staticmethod
    def _validate_coverage(start_ns: int | None, end_ns: int | None) -> None:
        if (start_ns is None) != (end_ns is None):
            raise ValidationError("coverage requires both event-time bounds")
        if start_ns is not None and end_ns is not None:
            require_ns(start_ns, "coverage_start_ns")
            require_ns(end_ns, "coverage_end_ns")
            if end_ns <= start_ns:
                raise TemporalConsistencyError("coverage interval must be positive")

    def health_as_of(self, provider_id: str, as_of_ns: int) -> SourceHealth:
        require_ns(as_of_ns, "as_of_ns")
        history = self._diagnostics.get(provider_id, ())
        latest = next((item for item in reversed(history) if item.as_of_ns <= as_of_ns), None)
        if latest is None:
            return SourceHealth(SourceHealthState.UNKNOWN)
        health = latest.health
        if (
            health.state is SourceHealthState.HEALTHY
            and health.last_success_ns is not None
            and as_of_ns - health.last_success_ns > self.settings.official.staleness_ns
        ):
            return replace(health, state=SourceHealthState.STALE)
        return health

    def _record(self, diagnostic: IngestionDiagnostics) -> IngestionDiagnostics:
        history = self._diagnostics.setdefault(diagnostic.provider_id, [])
        if history and diagnostic.as_of_ns < history[-1].as_of_ns:
            raise TemporalConsistencyError("provider attempts must be recorded in knowledge order")
        if history and diagnostic.as_of_ns == history[-1].as_of_ns:
            if history[-1] != diagnostic:
                raise PersistenceError("conflicting provider attempt at one time")
            return history[-1]
        history.append(diagnostic)
        return diagnostic

    def _source(self, item: OfficialItem, adapter: OfficialAdapter) -> SourceIdentity:
        return SourceIdentity(
            item.provider_id,
            item.source_version,
            adapter.adapter_version,
            SourceClass.OFFICIAL_PRIMARY,
        )

    @staticmethod
    def _facts(item: OfficialItem) -> tuple[StructuredFact, ...]:
        values: dict[str, str] = {"schedule_precision": item.precision.value}
        if item.scheduled_date is not None:
            values["schedule_date"] = item.scheduled_date
        if item.reference_period is not None:
            values.update(
                {
                    "period_start": item.reference_period.start_date,
                    "period_end": item.reference_period.end_date,
                    "period_frequency": item.reference_period.frequency.value,
                    "period_label": item.reference_period.label,
                }
            )
        if item.release_stage is not None:
            values["release_stage"] = item.release_stage
        if item.components:
            values["components"] = ",".join(item.components)
        return tuple(StructuredFact(key, values[key]) for key in sorted(values))

    def _raw(
        self, item: OfficialItem, adapter: OfficialAdapter, observed_ns: int, ingested_ns: int
    ) -> RawSourceObservation:
        metadata = tuple(sorted((("source_schema", item.source_schema), *item.source_metadata)))
        return RawSourceObservation(
            self._source(item, adapter),
            item.source_item_id,
            item.source_item_version,
            item.raw_fragment_hash,
            item.content_type,
            observed_ns,
            ingested_ns,
            published_at_ns=item.published_at_ns,
            source_reference=item.source_reference,
            raw_event_type=item.event_type.code,
            metadata=metadata,
            quality_flags=(
                frozenset({QualityFlag.MISSING_TIMESTAMP})
                if item.kind is ItemKind.RELEASE and item.published_at_ns is None
                else frozenset()
            ),
        )

    def _append_item(
        self, item: OfficialItem, adapter: OfficialAdapter, observed_ns: int, ingested_ns: int
    ) -> tuple[bool, bool, bool]:
        raw = self._raw(item, adapter, observed_ns, ingested_ns)
        existing_raw = self.index.store.get_observation(raw.observation_id)
        if existing_raw is not None:
            raw = existing_raw
            raw_created = False
        else:
            raw_created = self.index.append_observation(raw)
        identity = EventIdentity(
            item.event_type,
            EventScope(ScopeKind.MACRO_COUNTRY, ("US",)),
            item.semantic_key,
        )
        previous = self.index.store.get_event(identity.event_id, ingested_ns)
        history = self.index.store.get_revisions(identity.event_id)
        if any(raw.observation_id in event.evidence_observation_ids for event in history):
            return raw_created, False, False
        facts = self._facts(item)
        if (
            previous is not None
            and item.precision is TimePrecision.UNKNOWN
            and item.kind in (ItemKind.RELEASE, ItemKind.CORRECTION, ItemKind.CANCELLATION)
        ):
            facts = previous.facts
        if (
            item.kind is ItemKind.SCHEDULE
            and previous is not None
            and (
                previous.scheduled_for_ns == item.scheduled_for_ns
                and previous.facts == facts
                and previous.title == item.title
                and previous.lifecycle in (LifecycleState.SCHEDULED, LifecycleState.RESCHEDULED)
            )
        ):
            return raw_created, False, False
        if previous is not None and ingested_ns <= previous.revision.available_from_ns:
            raise TemporalConsistencyError("new official fact must follow previous knowledge time")
        number = len(history) + 1
        old_exact = self.index.resolve(identity.event_id, ingested_ns)
        schedule_kinds = (
            ItemKind.SCHEDULE,
            ItemKind.RESCHEDULE,
            ItemKind.SCHEDULE_CORRECTION,
        )
        conflict = False
        if item.kind in schedule_kinds:
            if previous is not None and previous.lifecycle in (
                LifecycleState.RELEASED,
                LifecycleState.UPDATED,
                LifecycleState.CANCELLED,
            ):
                raise ValidationError("schedule cannot follow released or cancelled event")
            if (
                previous is not None
                and previous.scheduled_for_ns is not None
                and (item.precision is not TimePrecision.EXACT_TIMESTAMP)
            ):
                raise ValidationError("official schedule precision downgrade requires adjudication")
            changed = (
                previous is not None
                and previous.scheduled_for_ns is not None
                and (previous.scheduled_for_ns != item.scheduled_for_ns)
            )
            if previous is None and item.kind is not ItemKind.SCHEDULE:
                raise ValidationError("first official schedule must be original")
            if item.kind is ItemKind.RESCHEDULE and previous is not None and not changed:
                raise ValidationError("reschedule requires changed exact timestamp")
            conflict = changed and item.kind is ItemKind.SCHEDULE
            if conflict and previous is not None:
                revision_kind, lifecycle = RevisionKind.UPDATE, LifecycleState.SCHEDULED
                schedule_time = previous.scheduled_for_ns
                facts = previous.facts
            elif item.kind is ItemKind.SCHEDULE_CORRECTION:
                revision_kind, lifecycle = RevisionKind.CORRECTION, LifecycleState.SCHEDULED
                schedule_time = item.scheduled_for_ns
            elif changed:
                revision_kind, lifecycle = RevisionKind.RESCHEDULE, LifecycleState.RESCHEDULED
                schedule_time = item.scheduled_for_ns
            else:
                revision_kind = RevisionKind.ORIGINAL if previous is None else RevisionKind.UPDATE
                lifecycle = LifecycleState.SCHEDULED
                schedule_time = item.scheduled_for_ns
        elif item.kind is ItemKind.CANCELLATION:
            if previous is None or previous.scheduled_for_ns is None:
                raise ValidationError("cancellation requires an existing exact schedule")
            revision_kind, lifecycle = RevisionKind.CANCELLATION, LifecycleState.CANCELLED
            schedule_time = previous.scheduled_for_ns
        elif item.kind is ItemKind.RELEASE:
            revision_kind = RevisionKind.ORIGINAL if previous is None else RevisionKind.UPDATE
            lifecycle = LifecycleState.RELEASED
            schedule_time = (
                previous.scheduled_for_ns if previous is not None else item.scheduled_for_ns
            )
        else:
            if previous is None or previous.lifecycle not in (
                LifecycleState.RELEASED,
                LifecycleState.UPDATED,
            ):
                raise ValidationError("correction requires a prior release")
            revision_kind, lifecycle = RevisionKind.CORRECTION, LifecycleState.UPDATED
            schedule_time = previous.scheduled_for_ns
        if item.kind in (ItemKind.RELEASE, ItemKind.CORRECTION) and (
            item.scheduled_for_ns is not None and schedule_time != item.scheduled_for_ns
        ):
            raise ValidationError("release schedule disagrees with known official schedule")
        evidence_ids = {raw.observation_id}
        claims: tuple[ScheduleClaim, ...] = ()
        selected_id = raw.observation_id
        if item.kind in (*schedule_kinds, ItemKind.CANCELLATION) and schedule_time is not None:
            claims = (
                ScheduleClaim(
                    raw.observation_id,
                    SourceClass.OFFICIAL_PRIMARY,
                    None if item.kind is ItemKind.CANCELLATION else schedule_time,
                    raw.ingested_at_ns,
                ),
            )
            if conflict:
                if old_exact is None:
                    raise ValidationError("official schedule conflict lacks prior exact claim")
                claims = tuple(
                    sorted((*old_exact.claims, *claims), key=lambda claim: claim.observation_id)
                )
                selected_id = old_exact.selected_observation_id
                evidence_ids.update(claim.observation_id for claim in claims)
        elif old_exact is not None:
            claims = old_exact.claims
            selected_id = old_exact.selected_observation_id
            evidence_ids.update(claim.observation_id for claim in claims)
        source_classes = frozenset({SourceClass.OFFICIAL_PRIMARY})
        flags = (
            frozenset({QualityFlag.MISSING_TIMESTAMP})
            if item.kind in (ItemKind.RELEASE, ItemKind.CORRECTION) and item.published_at_ns is None
            else frozenset()
        )
        if conflict:
            flags = frozenset({QualityFlag.CONFLICTING_SOURCES})
        event = CanonicalEvent(
            identity,
            EventRevision(number, revision_kind, ingested_ns, number - 1 if number > 1 else None),
            lifecycle,
            ConfirmationState.DISPUTED if conflict else ConfirmationState.CONFIRMED_PRIMARY,
            tuple(sorted(evidence_ids)),
            source_classes,
            previous.title if conflict and previous is not None else item.title,
            scheduled_for_ns=schedule_time,
            published_at_ns=item.published_at_ns,
            source_observed_at_ns=observed_ns,
            ingested_at_ns=ingested_ns,
            facts=facts,
            quality_flags=flags,
        )
        if schedule_time is not None and claims:
            self.index.append(
                ScheduleRevision(
                    event,
                    claims,
                    selected_id,
                    ("PRESERVE_PRIOR_PENDING_REVIEW",) if conflict else (),
                )
            )
            return raw_created, True, True
        self.index.store.append_event(event)
        return raw_created, True, False

    def ingest_payload(
        self,
        adapter: OfficialAdapter,
        payload: bytes,
        source_observed_at_ns: int,
        ingested_at_ns: int,
        *,
        coverage_start_ns: int | None = None,
        coverage_end_ns: int | None = None,
    ) -> IngestionDiagnostics:
        require_ns(source_observed_at_ns, "source_observed_at_ns")
        require_ns(ingested_at_ns, "ingested_at_ns")
        if source_observed_at_ns > ingested_at_ns:
            raise TemporalConsistencyError("official source observation follows ingestion")
        if adapter.provider_id not in self.settings.official.enabled_ids:
            raise ValidationError("official provider is disabled")
        if len(payload) > self.settings.official.max_payload_bytes:
            raise ValidationError("official payload exceeds configured maximum")
        self._validate_coverage(coverage_start_ns, coverage_end_ns)
        try:
            snapshot = adapter.parse(payload)
        except ProviderSchemaMismatchError:
            return self.report_failure(
                adapter,
                ingested_at_ns,
                FailureCategory.SCHEMA_MISMATCH,
                coverage_start_ns=coverage_start_ns,
                coverage_end_ns=coverage_end_ns,
            )
        except ProviderParseError:
            return self.report_failure(
                adapter,
                ingested_at_ns,
                FailureCategory.PARSE_ERROR,
                coverage_start_ns=coverage_start_ns,
                coverage_end_ns=coverage_end_ns,
            )
        except ValidationError:
            return self.report_failure(
                adapter,
                ingested_at_ns,
                FailureCategory.SCHEMA_MISMATCH,
                coverage_start_ns=coverage_start_ns,
                coverage_end_ns=coverage_end_ns,
            )
        if snapshot.provider_id != adapter.provider_id:
            raise ValidationError("adapter returned another provider's snapshot")
        if any(
            item.published_at_ns is not None and item.published_at_ns > source_observed_at_ns
            for item in snapshot.items
        ):
            raise TemporalConsistencyError("official publication follows source observation")
        raw_count = canonical_count = schedule_count = duplicate_count = 0
        for item in snapshot.items:
            raw_new, canonical_new, schedule_new = self._append_item(
                item, adapter, source_observed_at_ns, ingested_at_ns
            )
            raw_count += int(raw_new)
            canonical_count += int(canonical_new)
            schedule_count += int(schedule_new)
            duplicate_count += int(not canonical_new)
        prior = self.health_as_of(adapter.provider_id, ingested_at_ns)
        health = SourceHealth(
            SourceHealthState.HEALTHY,
            last_success_ns=ingested_at_ns,
            last_attempt_ns=ingested_at_ns,
            parse_failures=prior.parse_failures,
        )
        diagnostic = self._record(
            IngestionDiagnostics(
                adapter.provider_id,
                ingested_at_ns,
                len(snapshot.items),
                raw_count,
                canonical_count,
                schedule_count,
                duplicate_count,
                health,
            )
        )
        if coverage_start_ns is not None and coverage_end_ns is not None:
            self.index.append_coverage(
                CoverageRecord(
                    SourceIdentity(
                        adapter.provider_id,
                        snapshot.source_version,
                        adapter.adapter_version,
                        SourceClass.OFFICIAL_PRIMARY,
                    ),
                    coverage_start_ns,
                    coverage_end_ns,
                    ingested_at_ns,
                    health,
                )
            )
        return diagnostic

    def report_failure(
        self,
        adapter: OfficialAdapter,
        as_of_ns: int,
        category: FailureCategory,
        *,
        coverage_start_ns: int | None = None,
        coverage_end_ns: int | None = None,
    ) -> IngestionDiagnostics:
        require_ns(as_of_ns, "as_of_ns")
        if adapter.provider_id not in self.settings.official.enabled_ids:
            raise ValidationError("official provider is disabled")
        previous = self.health_as_of(adapter.provider_id, as_of_ns)
        self._validate_coverage(coverage_start_ns, coverage_end_ns)
        state = {
            FailureCategory.UNAVAILABLE: SourceHealthState.UNAVAILABLE,
            FailureCategory.RATE_LIMITED: SourceHealthState.RATE_LIMITED,
            FailureCategory.SCHEMA_MISMATCH: SourceHealthState.SCHEMA_MISMATCH,
            FailureCategory.PARSE_ERROR: SourceHealthState.DEGRADED,
        }[category]
        health = SourceHealth(
            state,
            last_success_ns=previous.last_success_ns,
            last_attempt_ns=as_of_ns,
            parse_failures=previous.parse_failures
            + int(category in (FailureCategory.SCHEMA_MISMATCH, FailureCategory.PARSE_ERROR)),
            error_category=category.value,
        )
        result = self._record(
            IngestionDiagnostics(adapter.provider_id, as_of_ns, 0, 0, 0, 0, 0, health, category)
        )
        if coverage_start_ns is not None and coverage_end_ns is not None:
            self.index.append_coverage(
                CoverageRecord(
                    SourceIdentity(
                        adapter.provider_id,
                        "unknown",
                        adapter.adapter_version,
                        SourceClass.OFFICIAL_PRIMARY,
                    ),
                    coverage_start_ns,
                    coverage_end_ns,
                    as_of_ns,
                    health,
                )
            )
        return result

    def fetch_and_ingest(
        self,
        adapter: OfficialAdapter,
        source_url: str,
        transport: HttpTransport,
        source_observed_at_ns: int,
        ingested_at_ns: int,
        *,
        coverage_start_ns: int | None = None,
        coverage_end_ns: int | None = None,
    ) -> IngestionDiagnostics:
        """One explicit fetch, with no implicit retries or wall-clock timestamping."""
        require_ns(source_observed_at_ns, "source_observed_at_ns")
        require_ns(ingested_at_ns, "ingested_at_ns")
        if adapter.provider_id not in self.settings.official.enabled_ids:
            raise ValidationError("official provider is disabled")
        if source_observed_at_ns > ingested_at_ns:
            raise TemporalConsistencyError("official source observation follows ingestion")
        self._validate_coverage(coverage_start_ns, coverage_end_ns)
        validate_source_url(adapter.provider_id, source_url)
        try:
            body = transport.get(
                source_url,
                timeout_seconds=self.settings.official.timeout_seconds,
                user_agent=self.settings.official.user_agent,
            )
        except ProviderRateLimitedError:
            return self.report_failure(
                adapter,
                ingested_at_ns,
                FailureCategory.RATE_LIMITED,
                coverage_start_ns=coverage_start_ns,
                coverage_end_ns=coverage_end_ns,
            )
        except ProviderTransportError:
            return self.report_failure(
                adapter,
                ingested_at_ns,
                FailureCategory.UNAVAILABLE,
                coverage_start_ns=coverage_start_ns,
                coverage_end_ns=coverage_end_ns,
            )
        return self.ingest_payload(
            adapter,
            body,
            source_observed_at_ns,
            ingested_at_ns,
            coverage_start_ns=coverage_start_ns,
            coverage_end_ns=coverage_end_ns,
        )
