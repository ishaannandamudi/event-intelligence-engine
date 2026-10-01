"""Offline corporate adapter and staged, provider-neutral ingestion."""

from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum

from eie.config.settings import Settings
from eie.core.errors import EIEError, PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.quality import QualityFlag
from eie.core.time import require_ns
from eie.corporate.index import CorporateEventIndex
from eie.corporate.models import (
    CorporateClaim,
    CorporateKind,
    Deal,
    Delisting,
    EarningsRelease,
    SpinOff,
    SymbolChange,
)
from eie.corporate.queries import CorporateCoverage
from eie.entities.sec import SecEntityRegistry, SecListingRevision, sec_listing_id
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.corporate.normalization import normalize_corporate
from eie.providers.corporate.parser import EXTRACT_VERSION, CorporateBatch, parse_batch
from eie.sources.models import RawSourceObservation, SourceHealth, SourceHealthState, SourceIdentity
from eie.sources.trust import SourceClass

ADAPTER_VERSION = "corporate-extract-1"


class CorporateEntityResolutionError(EIEError):
    """Stable corporate entity evidence is absent or inconsistent."""


class CorporateFailureCategory(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    PARSE_ERROR = "PARSE_ERROR"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"
    UNRESOLVED_ENTITY = "UNRESOLVED_ENTITY"


@dataclass(frozen=True, slots=True)
class CorporateDiagnostics:
    provider_id: str
    as_of_ns: int
    items_seen: int
    raw_created: int
    events_created_or_updated: int
    duplicates_suppressed: int
    conflicts: int
    health: SourceHealth
    error_category: CorporateFailureCategory | None = None

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if not self.provider_id or not isinstance(self.health, SourceHealth):
            raise ValidationError("corporate diagnostic provider and health required")
        for name in (
            "items_seen",
            "raw_created",
            "events_created_or_updated",
            "duplicates_suppressed",
            "conflicts",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError("corporate diagnostic count must be nonnegative")


class CorporateAdapter:
    """Accepts an audited EIE interchange extract, not an arbitrary vendor schema."""

    def __init__(
        self,
        provider_id: str,
        source_class: SourceClass,
        capabilities: frozenset[CorporateKind],
    ) -> None:
        from eie.corporate.models import PROVIDER_ID

        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValidationError("stable corporate provider ID required")
        if (
            not isinstance(source_class, SourceClass)
            or not isinstance(capabilities, frozenset)
            or any(not isinstance(kind, CorporateKind) for kind in capabilities)
        ):
            raise ValidationError("typed corporate adapter capabilities required")
        self.provider_id = provider_id
        self.source_class = source_class
        self._capabilities = capabilities

    def capabilities(self) -> frozenset[CorporateKind]:
        return self._capabilities

    def parse(self, payload: bytes) -> CorporateBatch:
        batch = parse_batch(payload, self.provider_id)
        if any(
            proposal.identity.kind not in self._capabilities
            for item in batch.items
            for proposal in item.proposals
        ) or any(kind not in self._capabilities for kind in batch.covered_kinds):
            raise ProviderSchemaMismatchError("corporate extract exceeds provider capabilities")
        return batch


class CorporateIngestor:
    """Copy-on-write corporate ingestion; one pointer commit after a whole batch."""

    def __init__(self, settings: Settings, registry: SecEntityRegistry | None = None) -> None:
        self.settings = settings
        self._index = CorporateEventIndex(settings, registry)
        self._diagnostics: dict[str, list[CorporateDiagnostics]] = {}
        self._claim_ids: dict[tuple[str, str], str] = {}
        self._last_attempt_ns: int | None = None

    @property
    def index(self) -> CorporateEventIndex:
        return self._index

    def diagnostics(self, provider_id: str) -> tuple[CorporateDiagnostics, ...]:
        return tuple(self._diagnostics.get(provider_id, ()))

    def health_as_of(self, provider_id: str, as_of_ns: int) -> SourceHealth:
        require_ns(as_of_ns, "as_of_ns")
        latest = next(
            (
                row
                for row in reversed(self._diagnostics.get(provider_id, ()))
                if row.as_of_ns <= as_of_ns
            ),
            None,
        )
        if latest is None:
            return SourceHealth(SourceHealthState.UNKNOWN)
        health = latest.health
        if (
            health.state is SourceHealthState.HEALTHY
            and health.last_success_ns is not None
            and as_of_ns - health.last_success_ns > self.settings.corporate.staleness_ns
        ):
            return replace(health, state=SourceHealthState.STALE)
        return health

    def _attempt(self, provider_id: str, at_ns: int) -> None:
        if not self.settings.corporate.enabled:
            raise ValidationError("corporate provider is disabled")
        require_ns(at_ns, "ingested_at_ns")
        if self._last_attempt_ns is not None and at_ns <= self._last_attempt_ns:
            raise TemporalConsistencyError("corporate attempts must advance in knowledge time")

    def _record(self, diagnostic: CorporateDiagnostics) -> CorporateDiagnostics:
        self._last_attempt_ns = diagnostic.as_of_ns
        self._diagnostics.setdefault(diagnostic.provider_id, []).append(diagnostic)
        return diagnostic

    def _failure(
        self, provider_id: str, as_of_ns: int, category: CorporateFailureCategory
    ) -> CorporateDiagnostics:
        previous = self.health_as_of(provider_id, as_of_ns)
        state = {
            CorporateFailureCategory.UNAVAILABLE: SourceHealthState.UNAVAILABLE,
            CorporateFailureCategory.RATE_LIMITED: SourceHealthState.RATE_LIMITED,
            CorporateFailureCategory.SCHEMA_MISMATCH: SourceHealthState.SCHEMA_MISMATCH,
            CorporateFailureCategory.PARSE_ERROR: SourceHealthState.DEGRADED,
            CorporateFailureCategory.NORMALIZATION_ERROR: SourceHealthState.DEGRADED,
            CorporateFailureCategory.UNRESOLVED_ENTITY: SourceHealthState.DEGRADED,
        }[category]
        health = SourceHealth(
            state,
            last_success_ns=previous.last_success_ns,
            last_attempt_ns=as_of_ns,
            parse_failures=previous.parse_failures
            + int(
                category
                in (CorporateFailureCategory.SCHEMA_MISMATCH, CorporateFailureCategory.PARSE_ERROR)
            ),
            error_category=category.value,
        )
        return self._record(
            CorporateDiagnostics(provider_id, as_of_ns, 0, 0, 0, 0, 0, health, category)
        )

    def report_failure(
        self, provider_id: str, as_of_ns: int, category: CorporateFailureCategory
    ) -> CorporateDiagnostics:
        self._attempt(provider_id, as_of_ns)
        return self._failure(provider_id, as_of_ns, category)

    @staticmethod
    def _apply_symbol_change(
        registry: SecEntityRegistry,
        issuer_id: str,
        change: SymbolChange,
        as_of_ns: int,
        observation_id: str,
    ) -> None:
        old = tuple(
            row
            for row in registry.listings_at(issuer_id, as_of_ns, change.effective_at_ns - 1)
            if row.security_id == change.security_id
            and row.ticker == change.old_ticker
            and row.exchange == change.old_exchange
        )
        if len(old) != 1:
            raise PersistenceError("symbol change needs one verified prior listing")
        previous = old[0]
        registry.add_listing(
            replace(
                previous,
                valid_to_ns=change.effective_at_ns,
                available_from_ns=as_of_ns,
                source_observation_id=observation_id,
            )
        )
        registry.add_listing(
            SecListingRevision(
                sec_listing_id(change.security_id, change.new_ticker, change.new_exchange),
                issuer_id,
                change.security_id,
                change.new_ticker,
                change.new_exchange,
                change.effective_at_ns,
                None,
                as_of_ns,
                observation_id,
            )
        )

    def ingest_payload(
        self, adapter: CorporateAdapter, payload: bytes, observed_ns: int, ingested_ns: int
    ) -> CorporateDiagnostics:
        self._attempt(adapter.provider_id, ingested_ns)
        require_ns(observed_ns, "source_observed_at_ns")
        if observed_ns > ingested_ns:
            raise TemporalConsistencyError("corporate observation follows ingestion")
        if len(payload) > self.settings.corporate.max_payload_bytes:
            raise ValidationError("corporate payload exceeds configured maximum")
        try:
            batch = adapter.parse(payload)
        except (ProviderSchemaMismatchError, ValidationError, TemporalConsistencyError):
            return self._failure(
                adapter.provider_id, ingested_ns, CorporateFailureCategory.SCHEMA_MISMATCH
            )
        except ProviderParseError:
            return self._failure(
                adapter.provider_id, ingested_ns, CorporateFailureCategory.PARSE_ERROR
            )
        if len(batch.items) > self.settings.corporate.max_items_per_batch:
            raise ValidationError("corporate batch exceeds configured maximum")
        staged = deepcopy(self._index)
        claim_ids = self._claim_ids.copy()
        raw_created = duplicates = updated = conflicts = 0
        grouped: dict[str, tuple[object, dict[tuple[str, str], CorporateClaim]]] = {}
        try:
            for item in batch.items:
                source = SourceIdentity(
                    adapter.provider_id, batch.source_version, ADAPTER_VERSION, adapter.source_class
                )
                raw = RawSourceObservation(
                    source,
                    item.item_id,
                    item.item_version,
                    item.payload_hash,
                    "application/json",
                    observed_ns,
                    ingested_ns,
                    published_at_ns=item.published_at_ns,
                    source_reference=item.source_reference,
                    raw_event_type=(
                        item.proposals[0].identity.kind.value
                        if len(item.proposals) == 1
                        else "CORPORATE_BUNDLE"
                    ),
                    raw_entity_identifiers=tuple(
                        sorted({proposal.identity.issuer_id for proposal in item.proposals})
                    ),
                    metadata=(("corporate_schema", EXTRACT_VERSION),),
                )
                previous_raw = staged.event_store.get_observation(raw.observation_id)
                if previous_raw is None:
                    staged.append_observation(raw)
                    raw_created += 1
                else:
                    raw = previous_raw
                    duplicates += 1
                for proposal in item.proposals:
                    identity = proposal.identity
                    if staged.registry.issuer_by_id(identity.issuer_id, ingested_ns) is None:
                        raise CorporateEntityResolutionError("corporate issuer is unresolved")
                    if identity.security_id is not None:
                        security = staged.registry.security_by_id(identity.security_id)
                        if security is None or security.issuer_id != identity.issuer_id:
                            raise CorporateEntityResolutionError(
                                "corporate security/issuer relationship unresolved"
                            )
                    if identity.listing_id is not None:
                        valid_at = proposal.effective_at_ns or ingested_ns
                        listings = staged.registry.listings_at(
                            identity.issuer_id, ingested_ns, valid_at
                        )
                        if not any(
                            listing.listing_id == identity.listing_id
                            and (
                                identity.security_id is None
                                or listing.security_id == identity.security_id
                            )
                            for listing in listings
                        ):
                            raise CorporateEntityResolutionError(
                                "corporate listing/issuer/security relationship unresolved"
                            )
                    if (
                        isinstance(proposal.payload, SymbolChange)
                        and proposal.payload.security_id != identity.security_id
                    ):
                        raise PersistenceError("symbol change identity needs matching security")
                    if (
                        isinstance(proposal.payload, SymbolChange)
                        and proposal.effective_at_ns != proposal.payload.effective_at_ns
                    ):
                        raise TemporalConsistencyError("symbol change effective times disagree")
                    if (
                        isinstance(proposal.payload, Delisting)
                        and proposal.payload.security_id != identity.security_id
                    ):
                        raise PersistenceError("delisting identity needs matching security")
                    if isinstance(proposal.payload, Deal) and identity.issuer_id not in (
                        proposal.payload.target_issuer_id,
                        proposal.payload.acquirer_issuer_id,
                    ):
                        raise PersistenceError("deal primary issuer must be a party")
                    if (
                        isinstance(proposal.payload, EarningsRelease)
                        and proposal.payload.published_at_ns > observed_ns
                    ):
                        raise TemporalConsistencyError("earnings release follows observation")
                    claim_key = (adapter.provider_id, proposal.provider_event_id)
                    known_id = claim_ids.get(claim_key)
                    if known_id is not None and known_id != identity.corporate_id:
                        raise PersistenceError("provider event ID changed corporate identity")
                    claim_ids[claim_key] = identity.corporate_id
                    claim = CorporateClaim(
                        adapter.provider_id,
                        proposal.provider_event_id,
                        raw.observation_id,
                        adapter.source_class,
                        ingested_ns,
                        proposal.payload,
                        proposal.status,
                        proposal.announced_at_ns,
                        proposal.effective_at_ns,
                        frozenset(
                            {QualityFlag.ENTITY_UNRESOLVED}
                            if (
                                isinstance(proposal.payload, Deal)
                                and any(
                                    party is None
                                    or staged.registry.issuer_by_id(party, ingested_ns) is None
                                    for party in (
                                        proposal.payload.target_issuer_id,
                                        proposal.payload.acquirer_issuer_id,
                                    )
                                )
                            )
                            or (
                                isinstance(proposal.payload, SpinOff)
                                and (
                                    proposal.payload.spun_issuer_id is None
                                    or staged.registry.issuer_by_id(
                                        proposal.payload.spun_issuer_id, ingested_ns
                                    )
                                    is None
                                )
                            )
                            else set()
                        ),
                        proposal.announced_date,
                    )
                    existing = grouped.get(identity.corporate_id)
                    if existing is None:
                        grouped[identity.corporate_id] = (identity, {claim.key: claim})
                    else:
                        prior_claim = existing[1].get(claim.key)
                        if prior_claim is not None and prior_claim != claim:
                            raise PersistenceError("conflicting same-batch corporate claim")
                        existing[1][claim.key] = claim
            for corporate_id in sorted(grouped):
                identity_obj, claim_map = grouped[corporate_id]
                from eie.corporate.models import CorporateIdentity

                if not isinstance(identity_obj, CorporateIdentity):
                    raise PersistenceError("invalid corporate identity")
                previous = staged.get(corporate_id, ingested_ns)
                normalized = normalize_corporate(
                    identity_obj,
                    tuple(claim_map[key] for key in sorted(claim_map)),
                    None if previous is None else previous.corporate,
                    staged.event_store,
                    self.settings,
                    ingested_ns,
                )
                if normalized is None:
                    duplicates += len(claim_map)
                    continue
                corporate, canonical = normalized
                if isinstance(corporate.selected_claim.payload, SymbolChange):
                    if (
                        previous is None
                        or previous.corporate.selected_claim.payload
                        != corporate.selected_claim.payload
                    ):
                        self._apply_symbol_change(
                            staged.registry,
                            identity_obj.issuer_id,
                            corporate.selected_claim.payload,
                            ingested_ns,
                            corporate.selected_claim.observation_id,
                        )
                staged.append(corporate, canonical)
                updated += 1
                conflicts += int(bool(corporate.conflict_reason_codes))
            if batch.coverage_from_date is not None and batch.coverage_to_date is not None:
                staged.append_coverage(
                    CorporateCoverage(
                        adapter.provider_id,
                        batch.covered_kinds,
                        batch.coverage_from_date,
                        batch.coverage_to_date,
                        ingested_ns,
                    )
                )
        except CorporateEntityResolutionError:
            return self._failure(
                adapter.provider_id, ingested_ns, CorporateFailureCategory.UNRESOLVED_ENTITY
            )
        except EIEError:
            return self._failure(
                adapter.provider_id, ingested_ns, CorporateFailureCategory.NORMALIZATION_ERROR
            )
        self._index = staged
        self._claim_ids = claim_ids
        previous_health = self.health_as_of(adapter.provider_id, ingested_ns)
        health = SourceHealth(
            SourceHealthState.HEALTHY,
            last_success_ns=ingested_ns,
            last_attempt_ns=ingested_ns,
            parse_failures=previous_health.parse_failures,
        )
        return self._record(
            CorporateDiagnostics(
                adapter.provider_id,
                ingested_ns,
                len(batch.items),
                raw_created,
                updated,
                duplicates,
                conflicts,
                health,
            )
        )
